"""Deterministic symbol and infrastructure rules for graph boundary detection.

Every rule maps a lowercase token (part of a symbol name or a container image)
to a canonical data store or external sink. Rules are plain data so the graph
export stays deterministic and easy to extend without touching graph logic.
"""

from __future__ import annotations

import re

# token fragment -> (boundary kind, canonical id, display label)
DATA_STORE_RULES: dict[str, tuple[str, str, str]] = {
    "asyncpg": ("sql", "postgres", "PostgreSQL"),
    "azure.storage": ("filesystem", "azure-blob", "Azure Blob Storage"),
    "boto3": ("filesystem", "s3", "Amazon S3"),
    "cassandra": ("nosql", "cassandra", "Cassandra"),
    "couchdb": ("nosql", "couchdb", "CouchDB"),
    "diesel": ("sql", "diesel", "SQL database (Diesel)"),
    "dynamodb": ("nosql", "dynamodb", "DynamoDB"),
    "elasticsearch": ("nosql", "elasticsearch", "Elasticsearch"),
    "entitymanager": ("sql", "jpa", "JPA persistence context"),
    "firestore": ("nosql", "firestore", "Firestore"),
    "fsspec": ("filesystem", "filesystem", "Filesystem storage"),
    "gcs": ("filesystem", "gcs", "Google Cloud Storage"),
    "google.cloud.storage": ("filesystem", "gcs", "Google Cloud Storage"),
    "gorm": ("sql", "gorm", "SQL database (GORM)"),
    "hibernate": ("sql", "jpa", "JPA/Hibernate"),
    "jakarta.persistence": ("sql", "jpa", "JPA/Hibernate"),
    "javax.persistence": ("sql", "jpa", "JPA/Hibernate"),
    "jdbc": ("sql", "jdbc", "JDBC database"),
    "jdbctemplate": ("sql", "jdbc", "JDBC database"),
    "kafka": ("queue", "kafka", "Kafka"),
    "knex": ("sql", "knex", "SQL database (Knex)"),
    "kombu": ("queue", "rabbitmq", "RabbitMQ"),
    "memcached": ("cache", "memcached", "Memcached"),
    "minio": ("filesystem", "s3", "S3-compatible object store"),
    "mongo": ("nosql", "mongodb", "MongoDB"),
    "mongoose": ("nosql", "mongodb", "MongoDB"),
    "mysql": ("sql", "mysql", "MySQL"),
    "nats": ("queue", "nats", "NATS"),
    "neo4j": ("nosql", "neo4j", "Neo4j"),
    "opensearch": ("nosql", "opensearch", "OpenSearch"),
    "pika": ("queue", "rabbitmq", "RabbitMQ"),
    "postgres": ("sql", "postgres", "PostgreSQL"),
    "prisma": ("sql", "prisma", "SQL database (Prisma)"),
    "psycopg": ("sql", "postgres", "PostgreSQL"),
    "pubsub": ("queue", "pubsub", "Pub/Sub"),
    "pymysql": ("sql", "mysql", "MySQL"),
    "rabbitmq": ("queue", "rabbitmq", "RabbitMQ"),
    "redis": ("cache", "redis", "Redis"),
    "s3": ("filesystem", "s3", "Amazon S3"),
    "sequelize": ("sql", "sequelize", "SQL database (Sequelize)"),
    "sqlalchemy": ("sql", "sqlalchemy", "SQL database (SQLAlchemy)"),
    "sqlite": ("sql", "sqlite", "SQLite"),
    "sqlmodel": ("sql", "sqlalchemy", "SQL database (SQLModel)"),
    "sqs": ("queue", "sqs", "Amazon SQS"),
    "sns": ("queue", "sns", "Amazon SNS"),
    "typeorm": ("sql", "typeorm", "SQL database (TypeORM)"),
}

# token fragment -> (boundary kind, canonical id, display label)
EXTERNAL_SINK_RULES: dict[str, tuple[str, str, str]] = {
    "auth0": ("http_api", "auth0", "Auth0 API"),
    "axios": ("http_api", "http", "HTTP API"),
    "fetch": ("http_api", "http", "HTTP API"),
    "github": ("http_api", "github", "GitHub API"),
    "got": ("http_api", "http", "HTTP API"),
    "http.client": ("http_api", "http", "HTTP API"),
    "httpx": ("http_api", "http", "HTTP API"),
    "javax.mail": ("smtp", "smtp", "SMTP mail"),
    "jakarta.mail": ("smtp", "smtp", "SMTP mail"),
    "mailgun": ("smtp", "mailgun", "Mailgun"),
    "nodemailer": ("smtp", "smtp", "SMTP mail"),
    "okhttp": ("http_api", "http", "HTTP API"),
    "openai": ("http_api", "openai", "OpenAI API"),
    "requests": ("http_api", "http", "HTTP API"),
    "resttemplate": ("http_api", "http", "HTTP API"),
    "sendgrid": ("smtp", "sendgrid", "SendGrid"),
    "ses": ("smtp", "ses", "Amazon SES"),
    "slack": ("webhook", "slack", "Slack API"),
    "smtplib": ("smtp", "smtp", "SMTP mail"),
    "stripe": ("http_api", "stripe", "Stripe API"),
    "superagent": ("http_api", "http", "HTTP API"),
    "twilio": ("http_api", "twilio", "Twilio API"),
    "urllib": ("http_api", "http", "HTTP API"),
    "webclient": ("http_api", "http", "HTTP API"),
    "webhook": ("webhook", "webhook", "Webhook"),
}

# Infrastructure resource type (Terraform) -> boundary rule
TERRAFORM_RULES: dict[str, tuple[str, str, str]] = {
    "aws_db_instance": ("sql", "rds", "AWS RDS"),
    "aws_dynamodb_table": ("nosql", "dynamodb", "DynamoDB"),
    "aws_elasticache_cluster": ("cache", "redis", "ElastiCache"),
    "aws_s3_bucket": ("filesystem", "s3", "Amazon S3"),
    "aws_sns_topic": ("queue", "sns", "Amazon SNS"),
    "aws_sqs_queue": ("queue", "sqs", "Amazon SQS"),
    "azurerm_redis_cache": ("cache", "redis", "Azure Cache for Redis"),
    "azurerm_storage_account": ("filesystem", "azure-blob", "Azure Blob Storage"),
    "google_sql_database_instance": ("sql", "postgres", "Google Cloud SQL"),
    "google_storage_bucket": ("filesystem", "gcs", "Google Cloud Storage"),
}


def _matches(fragment: str, lowered: str, *, allow_prefix: bool, split_words: bool) -> bool:
    """Match a rule fragment against dotted identifiers without short-fragment noise.

    Multi-part fragments match as a dotted subsequence; single-part fragments
    must equal one dotted segment, so ``ses`` never matches ``session`` and a
    sink rule never matches part of a snake_case identifier. Data store rules
    also accept underscore/hyphen word parts and, when long enough, a prefix of
    a segment; sink rules require the whole package segment.
    """
    if "." in fragment:
        return fragment in lowered
    separators = r"[._/@-]" if split_words else r"[./@-]"
    for segment in re.split(separators, lowered):
        if segment == fragment:
            return True
        if allow_prefix and len(fragment) >= 4 and segment.startswith(fragment):
            return True
    return False


_WRITE_VERBS = (
    "create",
    "delete",
    "insert",
    "merge",
    "publish",
    "produce",
    "put",
    "save",
    "send",
    "store",
    "update",
    "upsert",
    "write",
)


def classify_token(token: str) -> tuple[str, str, str, str] | None:
    """Classify a symbol token.

    Returns ``(boundary, kind, canonical, label)`` where boundary is
    ``datastore`` or ``sink``; ``None`` when no rule matches.
    """
    lowered = token.lower()
    for fragment in sorted(DATA_STORE_RULES):
        if _matches(fragment, lowered, allow_prefix=True, split_words=True):
            kind, canonical, label = DATA_STORE_RULES[fragment]
            return ("datastore", kind, canonical, label)
    for fragment in sorted(EXTERNAL_SINK_RULES):
        if _matches(fragment, lowered, allow_prefix=False, split_words=False):
            kind, canonical, label = EXTERNAL_SINK_RULES[fragment]
            return ("sink", kind, canonical, label)
    return None


def classify_image(image: str) -> tuple[str, str, str] | None:
    """Classify a container image name as a data store (image name is lowest priority)."""
    lowered = image.lower().split("@", 1)[0].split(":", 1)[0]
    for fragment in sorted(DATA_STORE_RULES):
        if _matches(fragment, lowered, allow_prefix=True, split_words=True):
            return DATA_STORE_RULES[fragment]
    return None


def classify_terraform(resource_type: str) -> tuple[str, str, str] | None:
    """Classify a Terraform resource type as a data store."""
    return TERRAFORM_RULES.get(resource_type)


def edge_kind_for_token(token: str, *, boundary_kind: str) -> str:
    """Pick reads/writes/publishes/http_request for a token and boundary."""
    if boundary_kind in ("http_api", "webhook", "smtp"):
        return "http_request"
    lowered = token.lower()
    if boundary_kind == "queue":
        return "publishes"
    if any(verb in lowered for verb in _WRITE_VERBS):
        return "writes"
    return "reads"
