# Analytics

Reads the Cowrie logs out of B2 and turns them into something you can ask
questions of. Runs on the k3s cluster, both as scheduled jobs and as the
environment behind Jupyter.

## Layers

```
bronze   the gzipped JSON lines in B2, untouched
silver   the same events as parquet on a longhorn pvc, partitioned by day
gold     features, clusters, survival curves
```

Bronze stays in B2 and is never copied down whole. Silver exists so that
exploring does not mean re-reading B2 on every query.

The normalize job that produces `silver/events` makes no modelling decisions.
It writes whatever columns Cowrie emitted that day, so the schema is Cowrie's,
documented at https://docs.cowrie.org/en/latest/OUTPUT.html. Two things are
not Cowrie's: `source_file`, the B2 object a row came from, and `year`,
`month`, `day`, which come from the directory names rather than the files.

Read it back through `db.silver_events_source()` rather than calling
`read_parquet` by hand. It pins `union_by_name`, without which the read breaks
once two days disagree on columns, and it pins the partition columns to
VARCHAR, which DuckDB otherwise guesses per column and gets inconsistent.

```python
import duckdb
from honeypot_analytics import db

con = duckdb.connect()
con.sql(f"SELECT eventid, count(*) FROM {db.silver_events_source()} GROUP BY 1")
```

Field sets differ sharply by event type. A day of scanning is almost entirely
session and login events, while the interesting ones (`file_download`,
`client.fingerprint`, `malformed_packet`) show up a handful of times and carry
fields nothing else does. That is why the job infers its schema from every
line rather than a sample.

## Running it

```
honeypot-analytics normalize                      yesterday
honeypot-analytics normalize --date 2026-09-15    one day
honeypot-analytics normalize --from 2026-09-13 --to 2026-09-15
```

Each run replaces one day's partition, so re-running is safe and backfilling
is a loop over dates.

## Environment

```
AWS_ACCESS_KEY_ID        b2 key id, read only
AWS_SECRET_ACCESS_KEY    b2 application key
HONEYPOT_B2_PREFIX       s3://cowrie-log/cowrie/honeypod1
HONEYPOT_B2_ENDPOINT     s3.us-west-004.backblazeb2.com
HONEYPOT_B2_REGION       us-west-004
HONEYPOT_DATA_DIR        where silver and gold are written
```

On the cluster the two credentials come from the `b2-credentials` sealed
secret and the rest from the chart's values.

## Development

```
pip install -e '.[dev]'
pytest
```

The tests build small gzipped JSON files locally and run the real normalize
path over them, so they cover the DuckDB SQL without touching B2.
