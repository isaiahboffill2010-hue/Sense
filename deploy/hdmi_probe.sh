#!/bin/bash
# hdmi_probe.sh - capture Pi + Sense state, for before/after HDMI comparison.
#
#   sudo bash deploy/hdmi_probe.sh before     # with NO HDMI connected
#   ... plug the HDMI cable in, wait ~5 s ...
#   sudo bash deploy/hdmi_probe.sh after
#   diff /tmp/sense-before.txt /tmp/sense-after.txt
#
# It also asks the running service for its own state dump (SIGUSR1), so the
# Python-side numbers land in the journal at the same moment.

LABEL="${1:-snapshot}"
OUT="/tmp/sense-${LABEL}.txt"

{
  echo "=============================================================="
  echo " SENSE / PI STATE : ${LABEL}"
  echo " $(date -Is)"
  echo "=============================================================="

  echo ""
  echo "--- display / HDMI -------------------------------------------"
  for status in /sys/class/drm/*/status; do
    [ -e "$status" ] && echo "  $(dirname "$status" | xargs basename): $(cat "$status")"
  done
  command -v tvservice >/dev/null && tvservice -s 2>/dev/null | sed 's/^/  /'
  echo "  DRM devices: $(ls /dev/dri 2>/dev/null | tr '\n' ' ')"

  echo ""
  echo "--- clocks / power / throttling ------------------------------"
  for clk in arm core v3d isp h264 hdmi; do
    printf "  %-6s %s\n" "$clk" "$(vcgencmd measure_clock $clk 2>/dev/null | cut -d= -f2)"
  done
  echo "  throttled   : $(vcgencmd get_throttled 2>/dev/null)"
  echo "  core volts  : $(vcgencmd measure_volts core 2>/dev/null | cut -d= -f2)"
  echo "  temp        : $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"

  echo ""
  echo "--- CPU governor / frequency ---------------------------------"
  for cpu in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
    [ -d "$cpu" ] || continue
    echo "  $(basename "$(dirname "$cpu")"): governor=$(cat "$cpu/scaling_governor" 2>/dev/null) cur=$(cat "$cpu/scaling_cur_freq" 2>/dev/null)"
  done

  echo ""
  echo "--- camera ---------------------------------------------------"
  echo "  /dev/video*: $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
  echo "  /dev/media*: $(ls /dev/media* 2>/dev/null | tr '\n' ' ')"
  timeout 10 rpicam-hello --list-cameras 2>&1 | sed 's/^/  /' | head -15

  echo ""
  echo "--- audio ----------------------------------------------------"
  aplay -l 2>&1 | sed 's/^/  /' | head -12

  echo ""
  echo "--- sense service --------------------------------------------"
  echo "  active      : $(systemctl is-active sense)"
  MAINPID=$(systemctl show -p MainPID --value sense)
  echo "  main pid    : ${MAINPID}"
  if [ "${MAINPID}" != "0" ] && [ -n "${MAINPID}" ]; then
    echo "  cpu/mem     : $(ps -o %cpu=,%mem=,etime= -p "${MAINPID}" 2>/dev/null)"
    echo "  threads     :"
    ps -L -o tid=,pcpu=,stat=,comm= -p "${MAINPID}" 2>/dev/null | sed 's/^/    /'
    echo "  wchan (what each thread is blocked on):"
    for tid in $(ls /proc/"${MAINPID}"/task 2>/dev/null); do
      echo "    tid ${tid}: $(cat /proc/"${MAINPID}"/task/"${tid}"/wchan 2>/dev/null)"
    done
  fi

  echo ""
  echo "--- asking Sense for its own state dump ----------------------"
  if [ "${MAINPID}" != "0" ] && [ -n "${MAINPID}" ]; then
    kill -USR1 "${MAINPID}" 2>/dev/null && echo "  SIGUSR1 sent - see: journalctl -u sense -n 60"
  else
    echo "  service not running"
  fi
} | tee "${OUT}"

echo ""
echo "Saved to ${OUT}"
