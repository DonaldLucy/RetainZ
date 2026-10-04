#!/bin/sh
# One fault per fresh FEMU VM. Both processes use the same live namespace;
# neither formats, resets all zones, nor reloads the VM between them.
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /tmp
FAULT=afterwrite
ZONE_BYTES=2097152
MAX_OPEN=4
MAX_ACTIVE=8
for opt in $(cat /proc/cmdline); do
 case "$opt" in
  retainz_fault=*) FAULT=${opt#retainz_fault=} ;;
  retainz_zone_bytes=*) ZONE_BYTES=${opt#retainz_zone_bytes=} ;;
  retainz_open=*) MAX_OPEN=${opt#retainz_open=} ;;
  retainz_active=*) MAX_ACTIVE=${opt#retainz_active=} ;;
 esac
done
fail() {
 echo "RETAINZ_RECOVERY_ERROR=$1"
 echo RETAINZ_RECOVERY_EXIT=1
 poweroff -f
 exit 1
}
case "$FAULT" in afterwrite|beforecommit) TEMPLATE=recover_precommit;; aftercommit) TEMPLATE=recover_postcommit;; *) fail unknown_fault;; esac
CKPT=$((ZONE_BYTES + ZONE_BYTES / 2))
FIRST=$((ZONE_BYTES / 3 + 7))
SECOND=$((CKPT - FIRST))
for stem in seed recover_precommit recover_postcommit; do
 sed -e "s/@CKPT@/$CKPT/g" -e "s/@FIRST@/$FIRST/g" -e "s/@SECOND@/$SECOND/g" \
  "/recovery/$stem.tsv.in" > "/tmp/$stem.tsv" || fail template
 done
sleep 2
echo "RETAINZ_RECOVERY_START fault=$FAULT zone_bytes=$ZONE_BYTES"
/online_bench /dev/nvme0n1 /tmp/seed.tsv retainz "$ZONE_BYTES" /tmp/seed \
 --meta-zones 3 --max-open "$MAX_OPEN" --max-active "$MAX_ACTIVE" \
 --fault "$FAULT" --fault-key pending
CODE=$?
echo "RETAINZ_CRASH_EXIT=$CODE"
[ "$CODE" -eq 86 ] || fail expected_exit_86
# A new process reconstructs identity/extents exclusively from on-device journal.
/online_bench /dev/nvme0n1 "/tmp/$TEMPLATE.tsv" retainz "$ZONE_BYTES" /tmp/recovered \
 --meta-zones 3 --max-open "$MAX_OPEN" --max-active "$MAX_ACTIVE" --recover
CODE=$?
echo "RETAINZ_RESTART_EXIT=$CODE"
[ "$CODE" -eq 0 ] || fail recovery_failed
if [ "$FAULT" != aftercommit ]; then
 /online_bench /dev/nvme0n1 /recovery/absent.tsv retainz "$ZONE_BYTES" /tmp/absent \
  --meta-zones 3 --max-open "$MAX_OPEN" --max-active "$MAX_ACTIVE" --recover
 CODE=$?
 echo "RETAINZ_UNCOMMITTED_RESTORE_EXIT=$CODE"
 [ "$CODE" -eq 1 ] || fail uncommitted_checkpoint_visible
fi
for stem in seed recovered absent; do
 if [ -f "/tmp/$stem.csv" ]; then
  echo "RETAINZ_RECOVERY_${stem}_CSV_BEGIN"
  cat "/tmp/$stem.csv"
  echo "RETAINZ_RECOVERY_${stem}_CSV_END"
 fi
done
echo "RETAINZ_RECOVERY_VERIFIED fault=$FAULT"
echo RETAINZ_RECOVERY_EXIT=0
poweroff -f
