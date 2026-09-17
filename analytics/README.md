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

From a notebook it is a view called `silver_events`, created by `init-views`
and stored in the workbench database:

```sql
%%sql
SELECT eventid, count(*) FROM silver_events GROUP BY 1 ORDER BY 2 DESC
```

From code that has its own connection, `db.silver_events_source()` is the same
thing as a SQL expression. Either way, do not hand write `read_parquet`: both
pin `union_by_name`, without which the read breaks once two days disagree on
columns, and pin the partition columns to VARCHAR, which DuckDB otherwise
guesses per column and gets inconsistent.

One process at a time can hold the workbench database open for writing, so
jobs connect in memory instead. Nothing a job does needs the file.

Field sets differ sharply by event type. A day of scanning is almost entirely
session and login events, while the interesting ones (`file_download`,
`client.fingerprint`, `malformed_packet`) show up a handful of times and carry
fields nothing else does. That is why the job infers its schema from every
line rather than a sample.

## Running it

```
honeypot-analytics init-views                     (re)create the notebook views
honeypot-analytics normalize                      yesterday
honeypot-analytics normalize --date 2026-09-15    one day
honeypot-analytics normalize --date ""              also yesterday, for argo
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
