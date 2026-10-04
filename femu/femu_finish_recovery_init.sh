#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /tmp
fail() { echo "RETAINZ_RECOVERY_ERROR=$1"; echo RETAINZ_RECOVERY_EXIT=1; poweroff -f; exit 1; }
for i in 1 2 3 4; do echo "C $i c$i $((i * 10)) short 2097152" >> /tmp/seed.tsv; done
echo Q >> /tmp/seed.tsv
printf 'R 5 c1\nR 5 c2\nR 5 c3\nQ\n' > /tmp/recover.tsv
printf 'C 6 next 10 short 2097152\nQ\n' > /tmp/orphan.tsv
printf 'R 7 c1\nC 7 later 10 short 2097152\nR 7 later\nQ\n' > /tmp/continue.tsv
sleep 2
OPTS='--meta-zones 3 --max-open 2 --max-active 4 --window 1'
/online_bench /dev/nvme0n1 /tmp/seed.tsv retainz 4194304 /tmp/seed $OPTS --fault afterfinishjournal
CODE=$?
echo "RETAINZ_CRASH_EXIT=$CODE"
[ "$CODE" -eq 86 ] || fail missing_finish_crash
/online_bench /dev/nvme0n1 /tmp/recover.tsv retainz 4194304 /tmp/recovered $OPTS --recover
CODE=$?
echo "RETAINZ_RESTART_EXIT=$CODE"
[ "$CODE" -eq 0 ] || fail finish_recovery
awk -F, 'NR==1 {for(i=1;i<=NF;i++)if($i=="stranded_tail_bytes")n=i} END {exit !($n==2097152)}' /tmp/recovered.csv || fail finish_not_replayed
/online_bench /dev/nvme0n1 /tmp/orphan.tsv retainz 4194304 /tmp/orphan $OPTS --recover --fault afterwrite --fault-key next
[ "$?" -eq 86 ] || fail missing_second_crash
/online_bench /dev/nvme0n1 /tmp/continue.tsv retainz 4194304 /tmp/continued $OPTS --recover
[ "$?" -eq 0 ] || fail subsequent_progress
for stem in seed recovered orphan continued; do
 echo "RETAINZ_RECOVERY_${stem}_CSV_BEGIN"
 cat "/tmp/$stem.csv"
 echo "RETAINZ_RECOVERY_${stem}_CSV_END"
done
echo 'RETAINZ_RECOVERY_VERIFIED fault=afterfinishjournal'
echo RETAINZ_RECOVERY_EXIT=0
poweroff -f
