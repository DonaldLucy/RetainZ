#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /tmp
FAULT=aftercopy
for opt in $(cat /proc/cmdline); do
 case "$opt" in retainz_fault=*) FAULT=${opt#retainz_fault=};; esac
done
fail() { echo "RETAINZ_RECOVERY_ERROR=$1"; echo RETAINZ_RECOVERY_EXIT=1; poweroff -f; exit 1; }
# 64 x 4 MiB, three metadata zones: reserve 54 full resident zones,
# then fill six zones with two independently retired objects each.
echo 'C 0 resident inf extreme 226492416' > /tmp/seed.tsv
for i in $(seq 1 12); do echo "C $i c$i $((i + 30)) short 2097152" >> /tmp/seed.tsv; done
for i in 1 3 5 7 9 11; do echo "D 13 c$i" >> /tmp/seed.tsv; done
echo 'C 14 interrupted 44 short 4194304' >> /tmp/seed.tsv
echo Q >> /tmp/seed.tsv
for i in 2 4 6 8 10 12; do echo "R 15 c$i" >> /tmp/recover.tsv; done
cat >> /tmp/recover.tsv <<'TRACE'
C 16 next 46 short 4194304
R 16 next
Q
TRACE
printf 'R 17 interrupted\nQ\n' > /tmp/absent.tsv
sleep 2
echo "RETAINZ_RECOVERY_START fault=$FAULT zone_bytes=4194304"
/online_bench /dev/nvme0n1 /tmp/seed.tsv fifo 4194304 /tmp/seed --meta-zones 3 --max-open 4 --max-active 8 --fault "$FAULT" --fault-key interrupted
CODE=$?
echo "RETAINZ_CRASH_EXIT=$CODE"
[ "$CODE" -eq 86 ] || fail missing_gc_crash
/online_bench /dev/nvme0n1 /tmp/recover.tsv fifo 4194304 /tmp/recovered --meta-zones 3 --max-open 4 --max-active 8 --recover
CODE=$?
echo "RETAINZ_RESTART_EXIT=$CODE"
[ "$CODE" -eq 0 ] || fail gc_recovery
/online_bench /dev/nvme0n1 /tmp/absent.tsv fifo 4194304 /tmp/absent --meta-zones 3 --max-open 4 --max-active 8 --recover
CODE=$?
echo "RETAINZ_UNCOMMITTED_RESTORE_EXIT=$CODE"
[ "$CODE" -eq 1 ] || fail uncommitted_visible
for stem in seed recovered absent; do
 echo "RETAINZ_RECOVERY_${stem}_CSV_BEGIN"
 cat "/tmp/$stem.csv"
 echo "RETAINZ_RECOVERY_${stem}_CSV_END"
done
echo "RETAINZ_RECOVERY_VERIFIED fault=$FAULT"
echo RETAINZ_RECOVERY_EXIT=0
poweroff -f
