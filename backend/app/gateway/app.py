import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.config import get_gateway_config
from app.gateway.csrf_middleware import CSRFMiddleware
from app.gateway.deps import langgraph_runtime
from app.gateway.routers import (
    agents,
    artifacts,
    assistants_compat,
    auth,
    channels,
    mcp,
    memory,
    models,
    runs,
    skills,
    suggestions,
    thread_runs,
    threads,
    uploads,
)
from deerflow.config.app_config import get_app_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


async def _ensure_admin_user(app: FastAPI) -> None:
    """Auto-create the admin user on first boot if no users exist.

    Prints the generated password to stdout so the operator can log in.
    On subsequent boots, warns if any user still needs setup.

    Multi-worker safe: relies on SQLite UNIQUE constraint to resolve races.
    Only the worker that successfully creates/updates the admin prints the
    password; losers silently skip.
    """
    import secrets

    from app.gateway.deps import get_local_provider

    provider = get_local_provider()
    user_count = await provider.count_users()

    if user_count == 0:
        password = secrets.token_urlsafe(16)
        try:
            admin = await provider.create_user(email="admin@deerflow.dev", password=password, system_role="admin", needs_setup=True)
        except ValueError:
            return  # Another worker already created the admin.

        # Migrate orphaned threads (no user_id) to this admin
        store = getattr(app.state, "store", None)
        if store is not None:
            await _migrate_orphaned_threads(store, str(admin.id))

        logger.info("=" * 60)
        logger.info("  Admin account created on first boot")
        logger.info("  Email:    %s", admin.email)
        logger.info("  Password: %s", password)
        logger.info("  Change it after login: Settings -> Account")
        logger.info("=" * 60)
        return

    # Admin exists but setup never completed — reset password so operator
    # can always find it in the console without needing the CLI.
    # Multi-worker guard: if admin was created less than 5s ago, another
    # worker just created it and will print the password — skip reset.
    admin = await provider.get_user_by_email("admin@deerflow.dev")
    if admin and admin.needs_setup:
        import time

        age = time.time() - admin.created_at.replace(tzinfo=UTC).timestamp()
        if age < 30:
            return  # Just created by another worker in this startup; its password is still valid.

        from app.gateway.auth.password import hash_password_async

        password = secrets.token_urlsafe(16)
        admin.password_hash = await hash_password_async(password)
        admin.token_version += 1
        await provider.update_user(admin)

        logger.info("=" * 60)
        logger.info("  Admin account setup incomplete — password reset")
        logger.info("  Email:    %s", admin.email)
        logger.info("  Password: %s", password)
        logger.info("  Change it after login: Settings -> Account")
        logger.info("=" * 60)


async def _migrate_orphaned_threads(store, admin_user_id: str) -> None:
    """Migrate threads with no user_id to the given admin."""
    try:
        migrated = 0
        results = await store.asearch(("threads",), limit=1000)
        for item in results:
            metadata = item.value.get("metadata", {})
            if not metadata.get("user_id"):
                metadata["user_id"] = admin_user_id
                item.value["metadata"] = metadata
                await store.aput(("threads",), item.key, item.value)
                migrated += 1
        if migrated:
            logger.info("Migrated %d orphaned thread(s) to admin", migrated)
    except Exception:
        logger.exception("Thread migration failed (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup
    try:
        get_app_config()
        logger.info("Configuration loaded successfully")
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # Initialize LangGraph runtime components (StreamBridge, RunManager, checkpointer, store)
    async with langgraph_runtime(app):
        logger.info("LangGraph runtime initialised")

        # Ensure admin user exists (auto-create on first boot)
        # Must run AFTER langgraph_runtime so app.state.store is available for thread migration
        await _ensure_admin_user(app)

        # Start IM channel service if any channels are configured
        try:
            from app.channels.service import start_channel_service

            channel_service = await start_channel_service()
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        yield

        # Stop channel service on shutdown
        try:
            from app.channels.service import stop_channel_service

            await stop_channel_service()
        except Exception:
            logger.exception("Failed to stop channel service")

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **MCP Configuration**: Manage Model Context Protocol (MCP) server configurations
- **Memory Management**: Access and manage global memory data for personalized conversations
- **Skills Management**: Query and manage skills and their enabled status
- **Artifacts**: Access thread artifacts and generated files
- **Health Monitoring**: System health check endpoints

### Architecture

LangGraph requests are handled by nginx reverse proxy.
This gateway provides custom endpoints for models, MCP configuration, skills, and artifacts.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Manage DeerFlow thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "assistants-compat",
                "description": "LangGraph Platform-compatible assistants API (stub)",
            },
            {
                "name": "runs",
                "description": "LangGraph Platform-compatible runs lifecycle (create, stream, cancel)",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # Auth: reject unauthenticated requests to non-public paths (fail-closed safety net)
    app.add_middleware(AuthMiddleware)

    # CSRF: Double Submit Cookie pattern for state-changing requests
    app.add_middleware(CSRFMiddleware)

    # CORS: when GATEWAY_CORS_ORIGINS is set (dev without nginx), add CORS middleware
    cors_origins_env = os.environ.get("GATEWAY_CORS_ORIGINS", "")
    if cors_origins_env:
        cors_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        # Validate: wildcard origin with credentials is a security misconfiguration
        for origin in cors_origins:
            if origin == "*":
                logger.error("GATEWAY_CORS_ORIGINS contains wildcard '*' with allow_credentials=True. This is a security misconfiguration — browsers will reject the response. Use explicit scheme://host:port origins instead.")
                cors_origins = [o for o in cors_origins if o != "*"]
                break
        if cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    # Include routers
    # Models API is mounted at /api/models
    app.include_router(models.router)

    # MCP API is mounted at /api/mcp
    app.include_router(mcp.router)

    # Memory API is mounted at /api/memory
    app.include_router(memory.router)

    # Skills API is mounted at /api/skills
    app.include_router(skills.router)

    # Artifacts API is mounted at /api/threads/{thread_id}/artifacts
    app.include_router(artifacts.router)

    # Uploads API is mounted at /api/threads/{thread_id}/uploads
    app.include_router(uploads.router)

    # Thread cleanup API is mounted at /api/threads/{thread_id}
    app.include_router(threads.router)

    # Agents API is mounted at /api/agents
    app.include_router(agents.router)

    # Suggestions API is mounted at /api/threads/{thread_id}/suggestions
    app.include_router(suggestions.router)

    # Channels API is mounted at /api/channels
    app.include_router(channels.router)

    # Assistants compatibility API (LangGraph Platform stub)
    app.include_router(assistants_compat.router)

    # Auth API is mounted at /api/v1/auth
    app.include_router(auth.router)

    # Thread Runs API (LangGraph Platform-compatible runs lifecycle)
    app.include_router(thread_runs.router)

    # Stateless Runs API (stream/wait without a pre-existing thread)
    app.include_router(runs.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict:
        """Health check endpoint.

        Returns:
            Service health status information.
        """
        return {"status": "healthy", "service": "deer-flow-gateway"}

    return app


# Create app instance for uvicorn
app = create_app()
