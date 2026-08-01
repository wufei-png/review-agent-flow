# SQLite and a database-backed Worker

The first deployment uses an API, one Worker, SQLite, and database polling. Storage and queue interfaces leave room for Postgres and Redis when multi-instance scale requires them. The implementation uses a fresh schema v2 with Review Runs, Auto-fix intents/relations, notification outbox and worktree cleanup records; pre-release databases are recreated rather than migrated by guessing old channel indexes.
