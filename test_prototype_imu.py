"""Hardware-free tests for the standalone IMU/head-motion prototype."""

import unittest

from prototype_imu import (
    AxisMapping,
    Calibration,
    HeadMotionClassifier,
    ImuError,
    ImuSample,
    MotionState,
    SafeLedVisualizer,
    calibrate_samples,
    collect_calibration,
)


def sample(gyro=(0.0, 0.0, 0.0), accel=(0.0, 0.0, 1.0), timestamp=1.0):
    return ImuSample(timestamp, *accel, *gyro)


CAL = Calibration((1.0, -2.0, 3.0), (0.0, 0.0, 1.0), 50)
MAP = AxisMapping(yaw_axis="z", yaw_sign=1, pitch_axis="y", pitch_sign=1)


class CalibrationTests(unittest.TestCase):
    def test_gyro_bias_and_resting_acceleration_are_means(self):
        values = [sample((1, -2, 3), (0.1, 0.2, 0.95)) for _ in range(12)]
        result = calibrate_samples(values)
        self.assertEqual(result.gyro_bias, (1.0, -2.0, 3.0))
        for actual, expected in zip(result.accel_reference, (0.1, 0.2, 0.95)):
            self.assertAlmostEqual(actual, expected)

    def test_missing_calibration_samples_are_rejected(self):
        with self.assertRaises(ImuError):
            calibrate_samples([sample()] * 3)

    def test_motion_during_calibration_is_rejected(self):
        values = [sample((0, 0, z)) for z in (-15, 15) * 6]
        with self.assertRaises(ImuError):
            calibrate_samples(values)

    def test_calibration_uses_mock_hardware_interface(self):
        class FakeImu:
            def __init__(self):
                self.reads = 0

            def read(self):
                self.reads += 1
                return sample((1, -2, 3))

            def close(self):
                pass

        hardware = FakeImu()
        result = collect_calibration(hardware, seconds=0.02, rate_hz=1000)
        self.assertGreaterEqual(hardware.reads, 10)
        self.assertEqual(result.gyro_bias, (1.0, -2.0, 3.0))


class MotionTests(unittest.TestCase):
    def classifier(self, **kwargs):
        return HeadMotionClassifier(CAL, MAP, smoothing_alpha=1.0, confirm_samples=2, **kwargs)

    def settle(self, classifier, value, count=3):
        result = None
        for _ in range(count):
            result = classifier.update(value)
        return result

    def test_stable_and_dead_zone(self):
        classifier = self.classifier()
        result = self.settle(classifier, sample((3, 5, 8)))
        self.assertEqual(result.motion_state, MotionState.STABLE)

    def test_left_and_right_rotation(self):
        classifier = self.classifier()
        left = self.settle(classifier, sample((1, -2, -40)))
        self.assertEqual(left.motion_state, MotionState.TURNING_LEFT)
        right = self.settle(classifier, sample((1, -2, 50)))
        self.assertEqual(right.motion_state, MotionState.TURNING_RIGHT)

    def test_upward_and_downward_tilt(self):
        classifier = self.classifier()
        up = self.settle(classifier, sample((1, 40, 3)))
        self.assertEqual(up.motion_state, MotionState.TILTING_UP)
        down = self.settle(classifier, sample((1, -45, 3)))
        self.assertEqual(down.motion_state, MotionState.TILTING_DOWN)

    def test_linear_or_unmapped_motion_is_moving(self):
        classifier = self.classifier()
        result = self.settle(classifier, sample((35, -2, 3)))
        self.assertEqual(result.motion_state, MotionState.MOVING)
        result = self.settle(classifier, sample((1, -2, 3), (0.4, 0, 1)))
        self.assertEqual(result.motion_state, MotionState.MOVING)

    def test_hysteresis_prevents_single_sample_flicker(self):
        classifier = self.classifier()
        self.assertEqual(classifier.update(sample((1, -2, 45))).motion_state, MotionState.STABLE)
        self.assertEqual(classifier.update(sample((1, -2, -45))).motion_state, MotionState.STABLE)
        self.assertEqual(classifier.update(sample((1, -2, 45))).motion_state, MotionState.STABLE)

    def test_exit_dead_zone_retains_motion_until_confirmed(self):
        classifier = self.classifier()
        self.settle(classifier, sample((1, -2, 45)))
        first_quiet = classifier.update(sample((1, -2, 10)))
        self.assertEqual(first_quiet.motion_state, MotionState.TURNING_RIGHT)
        second_quiet = classifier.update(sample((1, -2, 10)))
        self.assertEqual(second_quiet.motion_state, MotionState.STABLE)

    def test_smoothing_reduces_a_single_spike(self):
        classifier = HeadMotionClassifier(CAL, MAP, smoothing_alpha=0.2, confirm_samples=2)
        classifier.update(sample((1, -2, 3)))
        result = classifier.update(sample((1, -2, 63)))
        self.assertLess(result.gyro_z, 22.0)
        self.assertEqual(result.motion_state, MotionState.STABLE)

    def test_noisy_readings_around_dead_zone_remain_stable(self):
        classifier = self.classifier()
        result = None
        for z in (18, -19, 20, -17, 21, -20, 18):
            result = classifier.update(sample((1, -2, z + 3)))
        self.assertEqual(result.motion_state, MotionState.STABLE)

    def test_missing_read_is_uncertain_and_recovery_is_confirmed(self):
        classifier = self.classifier(recovery_samples=2)
        missing = classifier.missing(error="temporary read failure")
        self.assertEqual(missing.motion_state, MotionState.UNCERTAIN)
        self.assertIsNone(missing.gyro_x)
        first = classifier.update(sample((1, -2, 3)))
        self.assertEqual(first.motion_state, MotionState.UNCERTAIN)
        second = classifier.update(sample((1, -2, 3)))
        self.assertEqual(second.motion_state, MotionState.STABLE)

    def test_axis_and_sign_mapping_are_configurable(self):
        mapped = AxisMapping(yaw_axis="x", yaw_sign=-1, pitch_axis="z", pitch_sign=-1)
        classifier = HeadMotionClassifier(CAL, mapped, smoothing_alpha=1, confirm_samples=1)
        result = classifier.update(sample((41, -2, 3)))
        self.assertEqual(result.motion_state, MotionState.TURNING_LEFT)


class FakeLed:
    def __init__(self):
        self.closed = False

    def update(self, _state):
        raise OSError("LED unplugged")

    def close(self):
        self.closed = True


class LedFailureTests(unittest.TestCase):
    def test_led_failure_does_not_escape_or_stop_imu_logic(self):
        hardware = FakeLed()
        leds = SafeLedVisualizer(hardware)
        leds.update(MotionState.STABLE)
        self.assertIsNone(leds.device)
        self.assertTrue(hardware.closed)
        classifier = HeadMotionClassifier(CAL, MAP, smoothing_alpha=1, confirm_samples=1)
        self.assertEqual(classifier.update(sample((1, -2, 3))).motion_state, MotionState.STABLE)


if __name__ == "__main__":
    unittest.main()
