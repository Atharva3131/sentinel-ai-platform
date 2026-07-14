"""External infrastructure adapters."""

from backend.infrastructure.cosmos import (
    CosmosConnection,
    CosmosContainerFactory,
    CosmosPartitionStrategy,
    create_cosmos_client,
)
from backend.infrastructure.cosmos_repository import CosmosRepositoryBase
from backend.infrastructure.health_checks import (
    BlobHealthCheck,
    CosmosHealthCheck,
    Neo4jHealthCheck,
    RedisHealthCheck,
)
from backend.infrastructure.neo4j import (
    Neo4jConnection,
    Neo4jCypherExecutor,
    create_neo4j_driver,
    create_neo4j_retry_policy,
)
from backend.infrastructure.neo4j_repository import Neo4jRepositoryBase
from backend.infrastructure.redis import RedisConnection, create_redis_client

__all__ = [
    "BlobHealthCheck",
    "CosmosConnection",
    "CosmosContainerFactory",
    "CosmosHealthCheck",
    "CosmosPartitionStrategy",
    "CosmosRepositoryBase",
    "Neo4jConnection",
    "Neo4jCypherExecutor",
    "Neo4jHealthCheck",
    "Neo4jRepositoryBase",
    "RedisConnection",
    "RedisHealthCheck",
    "create_cosmos_client",
    "create_neo4j_driver",
    "create_neo4j_retry_policy",
    "create_redis_client",
]
