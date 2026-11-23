"""Known data-store and external-sink client symbols.

This is the single source of truth for the call targets that ``query flow-trace``
marks as data stores or external sinks. A symbol matches a pattern when its
lower-case fully-qualified name equals the pattern or ends with ``.<pattern>``,
so ``db.session.execute`` matches ``session.execute`` and
``sqlalchemy.create_engine`` matches ``create_engine``. The names in
``GLOBAL_CALLABLE_SYMBOLS`` denote language-global callables instead, so they
match only the exact name: builtin ``open`` is a sink but ``PIL.Image.open`` is
not. Patterns are intentionally specific: a wrong class is more harmful than an
unlabelled edge.
"""

from __future__ import annotations

DATA_STORE_CLIENT_SYMBOLS: tuple[str, ...] = (
    # SQL drivers, connections and query execution
    "create_engine",
    "psycopg2.connect",
    "psycopg.connect",
    "asyncpg.connect",
    "pymysql.connect",
    "mysql.connector.connect",
    "sqlite3.connect",
    "session.execute",
    "session.executemany",
    "session.query",
    "session.commit",
    "session.flush",
    "session.add",
    "session.merge",
    "cursor.execute",
    "cursor.executemany",
    "connection.execute",
    "conn.execute",
    "db.execute",
    "database.execute",
    "pool.query",
    "client.query",
    # JPA / JDBC / Spring Data
    "entityManager.persist",
    "entityManager.merge",
    "entityManager.find",
    "entityManager.remove",
    "jdbcTemplate.query",
    "jdbcTemplate.update",
    "jdbcTemplate.execute",
    "repository.save",
    "repository.saveAll",
    "repository.findById",
    "repository.delete",
    "repository.deleteById",
    # JavaScript / TypeScript persistence
    "prisma.client",
    "prisma.$queryRaw",
    "prisma.$executeRaw",
    "sequelize.query",
    "mongoose.connect",
    "mongoose.model",
    "mongoClient.db",
    # Cache and queue clients
    "redis.Redis",
    "redis.from_url",
    "redis.createClient",
    "ioredis",
    "KafkaProducer",
    "KafkaConsumer",
    "confluent_kafka.Producer",
    "confluent_kafka.Consumer",
    "pika.BlockingConnection",
    "aio_pika.connect_robust",
    # Filesystem persistence
    "open",
    "fs.writeFile",
    "fs.writeFileSync",
    "fs.readFile",
    "fs.readFileSync",
    "Path.write_text",
    "Path.read_text",
)

EXTERNAL_SINK_CLIENT_SYMBOLS: tuple[str, ...] = (
    # HTTP clients
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.patch",
    "requests.delete",
    "requests.head",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "httpx.put",
    "httpx.patch",
    "httpx.delete",
    "httpx.request",
    "urllib.request.urlopen",
    "fetch",
    "axios",
    "axios.get",
    "axios.post",
    "axios.put",
    "axios.patch",
    "axios.delete",
    "axios.request",
    "got",
    "RestTemplate",
    "WebClient",
    "HttpClient",
    "OkHttpClient",
    "URL.openConnection",
    # Mail and object stores
    "smtplib.SMTP",
    "sendgrid",
    "ses.sendEmail",
    "boto3.client",
    "boto3.resource",
    "s3.put_object",
    "s3.get_object",
    "stripe.Charge.create",
    # Messaging publishers treated as external sinks
    "sqs.send_message",
    "pubsub.v1.PublisherClient",
)

DATA_STORE = "datastore"
EXTERNAL_SINK = "external-sink"

GLOBAL_CALLABLE_SYMBOLS: frozenset[str] = frozenset({"axios", "fetch", "got", "open"})


def _matches(fqname: str, pattern: str) -> bool:
    """True when fqname is exactly pattern or ends with ``.pattern`` (case-insensitive)."""
    lowered = fqname.lower()
    pattern = pattern.lower()
    if pattern in GLOBAL_CALLABLE_SYMBOLS:
        return lowered == pattern
    return lowered == pattern or lowered.endswith(f".{pattern}")


def classify_sink(fqname: str) -> str | None:
    """Return ``datastore``/``external-sink`` for a known client symbol, else None."""
    for pattern in DATA_STORE_CLIENT_SYMBOLS:
        if _matches(fqname, pattern):
            return DATA_STORE
    for pattern in EXTERNAL_SINK_CLIENT_SYMBOLS:
        if _matches(fqname, pattern):
            return EXTERNAL_SINK
    return None
