#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /tmp/zenfs-aux
sleep 1
for opt in $(cat /proc/cmdline); do
 case "$opt" in
  rz_policy=*) POLICY=${opt#rz_policy=};;
  rz_zone=*) ZONE=${opt#rz_zone=};;
  rz_open=*) OPEN=${opt#rz_open=};;
  rz_active=*) ACTIVE=${opt#rz_active=};;
  rz_cadence=*) CADENCE=${opt#rz_cadence=};;
  rz_codec=*) CODEC=${opt#rz_codec=};;
  rz_timeout=*) LIMIT=${opt#rz_timeout=};;
  rz_borrow=*) BORROW=${opt#rz_borrow=};;
  rz_experiment=*) EXPERIMENT=${opt#rz_experiment=};;
  rz_variant=*) VARIANT=${opt#rz_variant=};;
  rz_window=*) WINDOW=${opt#rz_window=};;
  rz_sst=*) SST=${opt#rz_sst=};;
  rz_zenfs_resources=*) ZENFS_RESOURCES=${opt#rz_zenfs_resources=};;
 esac
done
echo RETAINZ_GUEST_START
if [ "${CODEC:-0}" = 1 ]; then
 /femu_codec_test /dev/nvme0n1 || { echo RETAINZ_CODEC_EXIT=1; poweroff -f; exit 1; }
 echo RETAINZ_CODEC_EXIT=0
fi
if [ "$POLICY" = zenfs ]; then
 echo mq-deadline > /sys/block/nvme0n1/queue/scheduler
 NATIVE_EXTRA=
 if [ "${SST:-0}" = 1 ]; then NATIVE_EXTRA=--sst-files; fi
 timeout -s TERM "${LIMIT:-570}" /zenfs_bench /dev/nvme0n1 /trace.tsv /result --format --gc=1 --cadence-ms=${CADENCE:-0} $NATIVE_EXTRA
elif [ "$POLICY" = span2 ]; then
 timeout -s TERM "${LIMIT:-570}" /online_bench /dev/nvme0n1 /trace.tsv retainz "$ZONE" /result --meta-zones 3 --max-open "$OPEN" --max-active "$ACTIVE" --window 2 --cadence-ms "${CADENCE:-0}"
else
 EXTRA=
 if [ "${ZENFS_RESOURCES:-0}" = 1 ]; then EXTRA=--zenfs-resources; fi
 if [ "$POLICY" = retainz_pressure ]; then POLICY=retainz; EXTRA="$EXTRA --pressure-fallback"; fi
 if [ "$POLICY" = coarse_pressure ]; then POLICY=coarse; EXTRA="$EXTRA --pressure-fallback"; fi
 if [ "${BORROW:-0}" = 1 ]; then EXTRA="$EXTRA --borrow-reserve"; fi
 if [ "${VARIANT:-normal}" = nearest ]; then EXTRA="$EXTRA --nearest"; fi
 if [ "${VARIANT:-normal}" = mix_anchors ]; then EXTRA="$EXTRA --mix-anchors"; fi
 if [ -n "${EXPERIMENT:-}" ]; then EXTRA="$EXTRA --experiment $EXPERIMENT"; fi
 MODE=--adaptive
 if [ "${VARIANT:-normal}" = resources ]; then MODE=--resource-adaptive; fi
 if [ -n "${WINDOW:-}" ]; then MODE="--window $WINDOW"; fi
 timeout -s TERM "${LIMIT:-570}" /online_bench /dev/nvme0n1 /trace.tsv "$POLICY" "$ZONE" /result --meta-zones 3 --max-open "$OPEN" --max-active "$ACTIVE" $MODE --cadence-ms "${CADENCE:-0}" $EXTRA
fi
CODE=$?
for result in /result.*; do
 echo RETAINZ_FILE_BEGIN=${result#/}
 cat "$result"
 echo RETAINZ_FILE_END=${result#/}
done
if [ "$CODE" -ne 0 ]; then
 # Checkpoint-space exhaustion must not destroy the last committed image.
 # The extra read occurs after the failed benchmark's metrics are finalized.
 if [ "$POLICY" = zenfs ]; then
  LAST=$(awk -F, 'NR>1 && $2=="C" && $8==1 && $3!="resident" {e=$1;k=$3} END {if(k!="")print e,k}' /result.events.csv)
 else
  LAST=$(awk -F, 'NR>1 && $1=="C" && $4=="ok" && $3!="resident" {e=$2;k=$3} END {if(k!="")print e,k}' /result.csv)
 fi
 if [ -n "$LAST" ]; then
  RECOVERY_EPOCH=$(awk '$1=="C" || $1=="D" || $1=="P" || $1=="R" {if($2>e)e=$2} END {print e+1}' /trace.tsv)
  LAST_KEY=${LAST#* }
  echo "R $RECOVERY_EPOCH $LAST_KEY" > /failure.tsv
  echo Q >> /failure.tsv
  if [ "$POLICY" = zenfs ]; then
   timeout -s TERM 60 /zenfs_bench /dev/nvme0n1 /failure.tsv /failure --mount $NATIVE_EXTRA > /failure.console 2>&1
  else
   RECOVERY_POLICY=$POLICY
   if [ "$POLICY" = span2 ]; then RECOVERY_POLICY=retainz; fi
   timeout -s TERM 60 /online_bench /dev/nvme0n1 /failure.tsv "$RECOVERY_POLICY" "$ZONE" /failure --meta-zones 3 --max-open "$OPEN" --max-active "$ACTIVE" --recover $EXTRA > /failure.console 2>&1
  fi
  RECOVER_CODE=$?
  sed 's/^/FAILURE_RESTORE_LOG /' /failure.console
  for result in /failure.csv /failure.events.csv /failure.summary.json; do
   if [ -f "$result" ]; then
    echo RETAINZ_FILE_BEGIN=${result#/}
    cat "$result"
    echo RETAINZ_FILE_END=${result#/}
   fi
  done
  echo RETAINZ_FAILURE_RESTORE_EXIT=$RECOVER_CODE
 fi
fi
echo RETAINZ_EXIT=$CODE
poweroff -f
