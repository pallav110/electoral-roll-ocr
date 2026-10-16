# Queue diagnostic report — 20261006T112712

Generated on the development laptop. The deployment described in the brief
(/home/spx015, http://192.168.0.15:8088) is a different machine; the queue
depths below are this laptop's, taken after restarting the stack.

## Services
beat  running  Up About a minute
db  running  Up 3 minutes (healthy)
minio  running  Up 33 minutes (healthy)
ocr  running  Up 3 minutes (healthy)
ocr-text  running  Up 3 minutes (healthy)
redis  running  Up 3 minutes (healthy)
web  running  Up About a minute
worker  running  Up About a minute

## Database status counts
   kind    |   status   | count 
-----------+------------+-------
 documents | completed  |     2
 documents | pending    |   582
 documents | processing |    19
 sessions  | completed  |     2
 sessions  | processing |    19
 units     | completed  |    11
 units     | processing |     2
 units     | queued     |   115
(8 rows)

## Totals
documents=603  approved=21  sessions=21  units=128  voter_records=149
## Redis
LLEN celery      : 246
DBSIZE           : 664

## Celery
    * {'id': '7454302e-094a-4286-9322-3d47d12d542b', 'name': 'app.tasks.process_unit', 'args': [UUID('d4577610-4145-406f-965f-b3856820f527'), UUID('ed1f5579-eeec-46d5-9e9a-fa8e67ffdfa3'), UUID('5cc3cdd2-68f9-4987-9862-4d0c82f9f59f')], 'kwargs': {}, 'type': 'app.tasks.process_unit', 'hostname': 'celery@3db3b5a7e860', 'time_start': 1791266132.480056, 'acknowledged': False, 'delivery_info': {'exchange': '', 'routing_key': 'celery', 'priority': 0, 'redelivered': False}, 'worker_pid': 8}

## Git
96b045c test ingestoin archive
e4ee4b5 Fix default value for 'approved' column in migration script
68b29b0  agaaainnnn IINCREASED LIMIT
?? docs/
