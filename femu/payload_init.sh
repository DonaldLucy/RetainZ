#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
sleep 1
for opt in $(cat /proc/cmdline); do
 case "$opt" in
  rz_policy=*) POLICY=${opt#rz_policy=};;
  rz_zone=*) ZONE=${opt#rz_zone=};;
  rz_open=*) OPEN=${opt#rz_open=};;
  rz_active=*) ACTIVE=${opt#rz_active=};;
 esac
done
EXTRA=--zenfs-resources
if [ "$POLICY" = retainz_pressure ]; then POLICY=retainz; EXTRA="$EXTRA --adaptive --pressure-fallback"; fi
echo RETAINZ_GUEST_START
/online_bench /dev/nvme0n1 /trace.tsv "$POLICY" "$ZONE" /result --meta-zones 3 --max-open "$OPEN" --max-active "$ACTIVE" $EXTRA --payload-image /dev/vda --payload-index /payload.index
CODE=$?
for result in /result.*; do
 echo RETAINZ_FILE_BEGIN=${result#/}
 cat "$result"
 echo RETAINZ_FILE_END=${result#/}
done
echo RETAINZ_PAYLOAD_WRITE_EXIT=$CODE
if [ "$CODE" = 0 ]; then
 /online_bench /dev/nvme0n1 /restore.tsv "$POLICY" "$ZONE" /payload_recovery --meta-zones 3 --max-open "$OPEN" --max-active "$ACTIVE" $EXTRA --recover --payload-image /dev/vda --restore-image /dev/vdb
 CODE=$?
 for result in /payload_recovery.*; do
  echo RETAINZ_FILE_BEGIN=${result#/}
  cat "$result"
  echo RETAINZ_FILE_END=${result#/}
 done
 echo RETAINZ_PAYLOAD_RESTORE_EXIT=$CODE
fi
echo RETAINZ_EXIT=$CODE
poweroff -f
