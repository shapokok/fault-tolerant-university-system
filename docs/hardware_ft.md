# Hardware Fault Tolerance: RAID and ECC (documentation only)

These two techniques protect the physical machine. They cannot be shown inside Docker on one
laptop, so they are described here and not implemented.

## RAID (Redundant Array of Independent Disks)
Several disks work together so that the loss of one disk does not lose data.

| Level | How it works | Survives | Usable space |
|---|---|---|---|
| RAID 0 | Data striped over disks, no copy | nothing (faster only) | 100% |
| RAID 1 | Every block written to 2 disks (mirror) | 1 disk failure | 50% |
| RAID 5 | Striping plus 1 parity block per stripe | 1 disk failure | (n-1)/n |
| RAID 6 | Striping plus 2 parity blocks | 2 disk failures | (n-2)/n |
| RAID 10 | Mirrors, then striped | 1 disk per mirror pair | 50% |

For our PostgreSQL servers we would choose **RAID 10**: databases do many small random writes,
RAID 10 handles them well, and rebuilding after a disk failure is fast (copy from the mirror,
no parity math). RAID 5 rebuilds are slow, and a second failure during the rebuild loses everything.

RAID is **not a backup**: a wrong `DELETE` or `TRUNCATE` is copied to every disk at once. This is
why the project also has `pg_dump` backups (`infra/backup.sh`). In our test, `TRUNCATE`
reached the replica within one second, and only the backup brought the data back.

## ECC memory (Error-Correcting Code)
RAM bits can flip because of cosmic rays, electrical noise or aging chips. ECC memory stores
extra check bits for every 64-bit word (a Hamming-style code, SECDED):
- **Single-bit error:** detected and corrected automatically, the program never notices.
- **Double-bit error:** detected, not corrected. The machine reports it and usually halts,
  which is better than silently writing corrupted data to the database.

Without ECC, one flipped bit in a payment amount held in memory can be written to disk and
replicated as if it were correct. Servers that run databases should always use ECC RAM.

## How they fit with what we built
| Layer | Fault | Protection |
|---|---|---|
| Memory | bit flip | ECC (documented) |
| Disk | disk dies | RAID 10 (documented) |
| Database server | server dies | streaming replica + `infra/failover.sh` (built) |
| Data | human error, corruption | `pg_dump` backups + `infra/restore.sh` (built) |
| Service process | crash | restart policy + second instance behind nginx (built) |
| Whole node | machine down | every service has a copy on node-b (built) |
