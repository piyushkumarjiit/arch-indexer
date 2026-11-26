"""Deterministic symbol and infrastructure rules for graph boundary detection.

Every rule maps a lowercase token (part of a symbol name or a container image)
to a canonical data store or external sink. Rules are plain data so the graph
export stays deterministic and easy to extend without touching graph logic.
"""

from __future__ import annotations

import re

from arch_indexer.packages import canonical_root, is_host_root, root_head

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
    "got": ("http_api", "http", "HTTP API"),
    "http.client": ("http_api", "http", "HTTP API"),
    "httpx": ("http_api", "http", "HTTP API"),
    "javax.mail": ("smtp", "smtp", "SMTP mail"),
    "jakarta.mail": ("smtp", "smtp", "SMTP mail"),
    "mailgun": ("smtp", "mailgun", "Mailgun"),
    "nodemailer": ("smtp", "smtp", "SMTP mail"),
    "okhttp": ("http_api", "http", "HTTP API"),
    "requests": ("http_api", "http", "HTTP API"),
    "sendgrid": ("smtp", "sendgrid", "SendGrid"),
    "ses": ("smtp", "ses", "Amazon SES"),
    "slack": ("webhook", "slack", "Slack API"),
    "smtplib": ("smtp", "smtp", "SMTP mail"),
    "stripe": ("http_api", "stripe", "Stripe API"),
    "superagent": ("http_api", "http", "HTTP API"),
    "twilio": ("http_api", "twilio", "Twilio API"),
    "urllib": ("http_api", "http", "HTTP API"),
    "webhook": ("webhook", "webhook", "Webhook"),
}

_OLLAMA = ("llm_api", "ollama", "Ollama")
_HTTP = ("http_api", "http", "HTTP API")
_GITHUB = ("http_api", "github", "GitHub API")


def _llm(canonical: str, vendor: str) -> tuple[str, str, str]:
    return ("llm_api", canonical, f"{vendor} LLM API")


# Anchored package prefix -> (boundary kind, canonical id, display label).
# A key matches the canonical root of a token (or a boundary-delimited prefix of
# the lowercased token); the longest key wins and its rule id is ``builtin:<key>``.
PACKAGE_SINK_RULES: dict[str, tuple[str, str, str]] = {
    "org.kohsuke.github": _GITHUB,
    "@octokit": _GITHUB,
    "github": _GITHUB,
    "java.net.http": _HTTP,
    "org.springframework.web.client": _HTTP,
    "org.springframework.web.reactive.function.client": _HTTP,
    "okhttp3": _HTTP,
    "org.apache.hc.client5": _HTTP,
    "org.apache.http": _HTTP,
    "openai": _llm("openai", "OpenAI"),
    "com.openai": _llm("openai", "OpenAI"),
    "langchain_openai": _llm("openai", "OpenAI"),
    "anthropic": _llm("anthropic", "Anthropic"),
    "com.anthropic": _llm("anthropic", "Anthropic"),
    "google.genai": _llm("google-genai", "Google GenAI"),
    "google.generativeai": _llm("google-genai", "Google GenAI"),
    "huggingface_hub": _llm("huggingface", "Hugging Face"),
    "dev.langchain4j": _llm("langchain4j", "LangChain4j"),
    "ollama": _OLLAMA,
    "langchain_ollama": _OLLAMA,
    "langchain_community.llms.ollama": _OLLAMA,
    "langchain_community.chat_models.ollama": _OLLAMA,
    "langchain_community.chat_models.chatollama": _OLLAMA,
    "langchain_community.embeddings.ollama": _OLLAMA,
    "langchain_community.embeddings.ollamaembeddings": _OLLAMA,
    "io.github.ollama4j": _OLLAMA,
}

_SQLITE = ("sql", "sqlite", "SQLite")


def _vector(canonical: str, label: str) -> tuple[str, str, str]:
    return ("vector_store", canonical, label)


PACKAGE_STORE_RULES: dict[str, tuple[str, str, str]] = {
    "faiss": _vector("faiss", "FAISS index"),
    "langchain_community.vectorstores.faiss": _vector("faiss", "FAISS index"),
    "node:sqlite": _SQLITE,
    "better-sqlite3": _SQLITE,
}
for _key, _canonical, _label in (
    ("chromadb", "chroma", "Chroma vector store"),
    ("qdrant_client", "qdrant", "Qdrant vector store"),
    ("pinecone", "pinecone", "Pinecone vector store"),
    ("weaviate", "weaviate", "Weaviate vector store"),
    ("lancedb", "lancedb", "LanceDB vector store"),
    ("pgvector", "pgvector", "pgvector vector store"),
):
    PACKAGE_STORE_RULES[_key] = _vector(_canonical, _label)
    PACKAGE_STORE_RULES[f"langchain_community.vectorstores.{_canonical}"] = _vector(
        _canonical, _label
    )
    PACKAGE_STORE_RULES[f"langchain_community.vectorstores.{_key}"] = _vector(_canonical, _label)


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
    separators = r"[._/@:-]" if split_words else r"[./@-]"
    for segment in re.split(separators, lowered):
        if segment == fragment:
            return True
        if allow_prefix and len(fragment) >= 4 and segment.startswith(fragment):
            return True
    return False


_VECTOR_WRITE_VERBS = (
    "save_local",
    "add_documents",
    "add_texts",
    "from_documents",
    "from_texts",
    "upsert",
    "persist",
)

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


_DELIMITERS = (".", "/", ":", "#")


def _package_key_matches(key: str, root: str, lowered: str) -> bool:
    """Anchored, boundary-delimited match of a package key; never a bare ``startswith``."""
    if is_host_root(root) and "/" not in key:
        return False
    if root == key or root.startswith((f"{key}.", f"{key}/")):
        return True
    return lowered == key or lowered.startswith(tuple(f"{key}{d}" for d in _DELIMITERS))


def _longest_package_rule(
    rules: dict[str, tuple[str, str, str]], root: str, lowered: str
) -> tuple[str, tuple[str, str, str]] | None:
    best: str | None = None
    for key in rules:
        if _package_key_matches(key, root, lowered) and (best is None or len(key) > len(best)):
            best = key
    return None if best is None else (best, rules[best])


def classify_token_rule(token: str) -> tuple[str, str, str, str, str] | None:
    """Classify a symbol token and say which rule matched.

    Returns ``(boundary, kind, canonical, label, rule_id)``; ``None`` when no rule matches.
    """
    lowered = token.lower()
    root = canonical_root(token)
    hit = _longest_package_rule(PACKAGE_STORE_RULES, root, lowered)
    if hit is not None:
        key, (kind, canonical, label) = hit
        return ("datastore", kind, canonical, label, f"builtin:{key}")
    for fragment in sorted(DATA_STORE_RULES):
        if _matches(fragment, lowered, allow_prefix=True, split_words=True):
            kind, canonical, label = DATA_STORE_RULES[fragment]
            return ("datastore", kind, canonical, label, f"builtin:{fragment}")
    hit = _longest_package_rule(PACKAGE_SINK_RULES, root, lowered)
    if hit is not None:
        key, (kind, canonical, label) = hit
        return ("sink", kind, canonical, label, f"builtin:{key}")
    head = root_head(root)
    for fragment in sorted(EXTERNAL_SINK_RULES):
        if "." not in fragment and head == fragment:
            kind, canonical, label = EXTERNAL_SINK_RULES[fragment]
            return ("sink", kind, canonical, label, f"builtin:{fragment}")
    for fragment in sorted(EXTERNAL_SINK_RULES):
        if "." in fragment and (
            lowered == fragment or lowered.startswith(tuple(f"{fragment}{d}" for d in _DELIMITERS))
        ):
            kind, canonical, label = EXTERNAL_SINK_RULES[fragment]
            return ("sink", kind, canonical, label, f"builtin:{fragment}")
    return None


def classify_token(token: str) -> tuple[str, str, str, str] | None:
    """Classify a symbol token.

    Returns ``(boundary, kind, canonical, label)`` where boundary is
    ``datastore`` or ``sink``; ``None`` when no rule matches.
    """
    rule = classify_token_rule(token)
    return None if rule is None else rule[:4]


def classify_image(image: str) -> tuple[str, str, str] | None:
    """Classify a container image name as a data store (image name is lowest priority)."""
    lowered = re.sub(r":[^/]*$", "", image.lower().split("@", 1)[0])
    for fragment in sorted(DATA_STORE_RULES):
        if _matches(fragment, lowered, allow_prefix=True, split_words=True):
            return DATA_STORE_RULES[fragment]
    return None


def classify_terraform(resource_type: str) -> tuple[str, str, str] | None:
    """Classify a Terraform resource type as a data store."""
    return TERRAFORM_RULES.get(resource_type)


def edge_kind_for_token(token: str, *, boundary_kind: str) -> str:
    """Pick reads/writes/publishes/http_request for a token and boundary."""
    if boundary_kind in ("http_api", "webhook", "smtp", "llm_api"):
        return "http_request"
    lowered = token.lower()
    if boundary_kind == "vector_store":
        # Checked before the generic verb scan: "vectorstores" contains "store".
        return "writes" if any(verb in lowered for verb in _VECTOR_WRITE_VERBS) else "reads"
    if boundary_kind == "queue":
        return "publishes"
    if any(verb in lowered for verb in _WRITE_VERBS):
        return "writes"
    return "reads"
