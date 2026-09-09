"""MAX Messenger platform adapter for Hermes Agent.

Connects Hermes to a MAX bot via the ``maxapi`` library. Supports text,
media, inline callback buttons, typing indicators and cron delivery.

Configuration in config.yaml::

    gateway:
      platforms:
        max:
          enabled: true
          extra:
            token: "..."                # optional; MAX_BOT_TOKEN env var is preferred
            poll_timeout: 30            # long-polling timeout
            reconnect_backoff: [2, 5, 10, 30, 60]

Or via environment variables::

    MAX_BOT_TOKEN=...
    MAX_ALLOWED_USERS=12345,67890
    MAX_ALLOW_ALL_USERS=1
    MAX_HOME_CHANNEL=12345
    MAX_HOME_CHANNEL_NAME="My Home"
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import maxapi
    from maxapi import Bot
    from maxapi.enums.sender_action import SenderAction
    from maxapi.enums.update import UpdateType
    from maxapi.methods.types.getted_updates import process_update_request
    from maxapi.types.attachments import AttachmentInput
    from maxapi.types.attachments.attachment import (
        Attachment,
        ButtonsPayload,
        PhotoAttachmentPayload,
    )
    from maxapi.types.attachments.buttons import CallbackButton
    from maxapi.enums.intent import Intent
    from maxapi.types.attachments.file import File
    from maxapi.types.attachments.image import Image
    from maxapi.types.attachments.upload import AttachmentUpload
    from maxapi.types.attachments.video import Video
    from maxapi.types.callback import Callback
    from maxapi.types.input_media import InputMedia, InputMediaBuffer
    from maxapi.types.message import Message, NewMessageLink
    from maxapi.types.updates import UpdateUnion
    from maxapi.types.updates.bot_added import BotAdded
    from maxapi.types.updates.bot_started import BotStarted
    from maxapi.types.updates.bot_stopped import BotStopped
    from maxapi.types.updates.message_callback import MessageCallback
    from maxapi.types.updates.message_created import MessageCreated
    from maxapi.types.updates.message_edited import MessageEdited
    from maxapi.types.updates.message_removed import MessageRemoved

    MAXAPI_AVAILABLE = True
except Exception as _maxapi_import_exc:  # pragma: no cover - dependency gate
    logger.debug("maxapi import failed: %s", _maxapi_import_exc)
    MAXAPI_AVAILABLE = False
    Bot = None  # type: ignore[misc, assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _strip_media_directives as _strip_media_tag_directives,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_media_bytes,
    cache_video_from_bytes,
    safe_url_for_log,
    should_send_media_as_audio,
)
from gateway.session import SessionSource

# MAX message text limit. The exact server-side cap is undocumented; we use a
# safe value and split longer responses into multiple messages.
MAX_MESSAGE_LENGTH = 4000

# Default long-polling timeout in seconds.
DEFAULT_POLL_TIMEOUT = 30

# Reconnect backoff schedule in seconds.
DEFAULT_RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# Dedup window for inbound updates.
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 2000

# Callback payload prefixes shared with other Hermes adapters.
_CLARIFY_PREFIX = "cl"
_APPROVAL_PREFIX = "appr"
_SLASH_CONFIRM_PREFIX = "sc"


def _safe_id(value: Any, keep: int = 8) -> str:
    """Return a short, log-safe identifier."""
    raw = str(value or "").strip()
    if not raw:
        return "?"
    return raw if len(raw) <= keep else raw[:keep]


def _get_token(config: PlatformConfig) -> Optional[str]:
    """Resolve MAX bot token from env or config."""
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if token:
        return token
    extra = getattr(config, "extra", {}) or {}
    token = (extra.get("token") or "").strip()
    return token or None


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_backoff(value: Any) -> List[int]:
    """Parse a comma-separated backoff list or Python list."""
    if isinstance(value, (list, tuple)):
        return [int(x) for x in value if x is not None]
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        try:
            return [int(p) for p in parts]
        except ValueError:
            pass
    return list(DEFAULT_RECONNECT_BACKOFF)


def _parse_allowed_users(value: Any) -> set[str]:
    """Parse a comma-separated allowlist of user IDs.

    Supports integer IDs, string IDs and a wildcard ``*``. Empty input means
    no explicit allowlist is configured.
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        return {str(u).strip() for u in value if str(u).strip()}
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    return set()


def _is_truthy(value: Any) -> bool:
    """Return True for truthy environment-style booleans."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _resolve_attachment_url(attachment: Attachment) -> Optional[str]:
    """Best-effort extraction of a downloadable URL from a MAX attachment."""
    if isinstance(attachment, Video):
        urls = attachment.urls
        if urls:
            for quality in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240", "mp4_144"):
                url = getattr(urls, quality, None)
                if url:
                    return url
        if attachment.payload and isinstance(attachment.payload, PhotoAttachmentPayload):
            return attachment.payload.url
        return None

    if isinstance(attachment, File):
        return attachment.payload.url if attachment.payload else None

    if isinstance(attachment, Image):
        payload = attachment.payload
        if isinstance(payload, PhotoAttachmentPayload):
            return payload.url
        if payload:
            return getattr(payload, "url", None)
        return None

    # Audio / sticker / share / contact / location / buttons
    payload = getattr(attachment, "payload", None)
    if payload:
        return getattr(payload, "url", None)
    return None


def _resolve_attachment_filename(attachment: Attachment, url: Optional[str] = None) -> str:
    """Best-effort filename for caching an attachment."""
    if isinstance(attachment, File) and attachment.filename:
        return attachment.filename
    if url:
        name = Path(url.split("?")[0]).name
        if name:
            return name
    ext = ".bin"
    if isinstance(attachment, Image):
        ext = ".jpg"
    elif isinstance(attachment, Video):
        ext = ".mp4"
    return f"max_{uuid.uuid4().hex[:12]}{ext}"


def check_requirements() -> bool:
    """Return True when maxapi is installed and a token is configured."""
    if not MAXAPI_AVAILABLE:
        return False
    return bool(os.getenv("MAX_BOT_TOKEN", "").strip())


def validate_config(config) -> bool:
    """Return True when the platform has a token configured."""
    return bool(_get_token(config))


def is_connected(config) -> bool:
    """Return True when max is configured (env or config.yaml)."""
    return bool(_get_token(config))


class MaxAdapter(BasePlatformAdapter):
    """Hermes platform adapter for MAX Messenger."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    interactive_resume: bool = True

    def __init__(self, config: PlatformConfig):
        platform = Platform("max")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self._token: Optional[str] = _get_token(config)
        self._poll_timeout: int = _coerce_int(
            os.getenv("MAX_POLL_TIMEOUT") or extra.get("poll_timeout"),
            DEFAULT_POLL_TIMEOUT,
        )
        self._reconnect_backoff: List[int] = _parse_backoff(
            os.getenv("MAX_RECONNECT_BACKOFF") or extra.get("reconnect_backoff")
        )
        self._api_url: Optional[str] = (
            os.getenv("MAX_API_URL", "").strip() or extra.get("api_url") or None
        )

        # User authorization: explicit allowlist plus an opt-in allow-all flag.
        # Default-deny when neither is configured, so the bot is safe by default.
        self._allowed_users: set[str] = _parse_allowed_users(
            os.getenv("MAX_ALLOWED_USERS") or extra.get("allow_from")
        )
        self._allow_all_users: bool = _is_truthy(
            os.getenv("MAX_ALLOW_ALL_USERS") or extra.get("allow_all")
        )

        self._bot: Optional[Bot] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._connected: bool = False

        # Inbound update/message deduplication.
        self._seen_updates: Dict[str, float] = {}

        # Track our own bot user id to filter self-echo.
        self._bot_user_id: Optional[int] = None

    @property
    def name(self) -> str:
        return "MAX"

    def _is_user_allowed(self, user_id: Optional[Any]) -> bool:
        """Return True when the user is authorized to talk to the bot.

        Authorization order:
        1. ``MAX_ALLOW_ALL_USERS=true`` (or config ``allow_all``) → allow.
        2. Explicit allowlist configured → allow only listed IDs.
        3. No allowlist and no allow-all → deny (secure default).
        """
        if self._allow_all_users:
            return True
        if "*" in self._allowed_users:
            return True
        if not self._allowed_users:
            return False
        if user_id is None:
            return False
        return str(user_id).strip() in self._allowed_users

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to MAX via long polling."""
        if not MAXAPI_AVAILABLE:
            logger.error("[%s] maxapi is not installed", self.name)
            return False
        if not self._token:
            logger.error("[%s] MAX_BOT_TOKEN is not configured", self.name)
            self._set_fatal_error(
                "config_missing",
                "MAX_BOT_TOKEN must be set",
                retryable=False,
            )
            return False

        try:
            self._bot = Bot(token=self._token)
            if self._api_url:
                self._bot.set_api_url(self._api_url)
        except Exception as e:
            logger.error("[%s] Failed to initialize maxapi Bot: %s", self.name, e)
            self._set_fatal_error("bot_init_failed", str(e), retryable=False)
            return False

        try:
            me = await self._bot.get_me()
            self._bot_user_id = getattr(me, "user_id", None)
            logger.info(
                "[%s] Bot identity: id=%s name=%s",
                self.name,
                self._bot_user_id,
                getattr(me, "first_name", "?"),
            )
        except Exception as e:
            logger.warning("[%s] get_me failed: %s", self.name, e)

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        self._connected = True
        logger.info("[%s] Connected (polling, timeout=%ds)", self.name, self._poll_timeout)
        return True

    async def disconnect(self) -> None:
        """Disconnect from MAX and stop polling."""
        self._running = False
        self._connected = False
        self._mark_disconnected()

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._bot is not None:
            try:
                await self._bot.close_session()
            except Exception as e:
                logger.debug("[%s] Error closing bot session: %s", self.name, e)
            self._bot = None

        self._seen_updates.clear()
        logger.info("[%s] Disconnected", self.name)

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Long-polling loop with automatic reconnect."""
        backoff_idx = 0
        marker: Optional[int] = None

        while self._running:
            loop_start = time.monotonic()
            try:
                raw = await self._bot.get_updates(
                    limit=100,
                    timeout=self._poll_timeout,
                    marker=marker,
                )
                # Reset backoff on a successful poll.
                if time.monotonic() - loop_start >= 5.0:
                    backoff_idx = 0

                events = await process_update_request(raw, self._bot)
                for event in events:
                    try:
                        await self._dispatch_update(event)
                    except Exception as e:
                        logger.exception("[%s] Error dispatching update: %s", self.name, e)

                # Advance marker if the response contains one.
                new_marker = raw.get("marker")
                if new_marker is not None:
                    marker = int(new_marker)
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Polling error: %s", self.name, e)
                delay = self._reconnect_backoff[min(backoff_idx, len(self._reconnect_backoff) - 1)]
                logger.info("[%s] Reconnecting in %ds...", self.name, delay)
                await asyncio.sleep(delay)
                backoff_idx += 1

    # ------------------------------------------------------------------
    # Inbound dispatch
    # ------------------------------------------------------------------

    async def _dispatch_update(self, event: UpdateUnion) -> None:
        """Route a single MAX update to the appropriate handler."""
        update_type = getattr(event, "update_type", None)

        if isinstance(event, MessageCreated):
            await self._on_message_created(event)
        elif isinstance(event, MessageCallback):
            await self._on_message_callback(event)
        elif isinstance(event, (BotStarted, BotAdded)):
            await self._on_bot_started_or_added(event)
        elif isinstance(event, BotStopped):
            logger.debug("[%s] Bot stopped in chat %s", self.name, getattr(event, "chat_id", None))
        elif isinstance(event, MessageEdited):
            logger.debug("[%s] Message edited: %s", self.name, getattr(event, "message", None))
        elif isinstance(event, MessageRemoved):
            logger.debug("[%s] Message removed: %s", self.name, getattr(event, "message_id", None))
        else:
            logger.debug("[%s] Ignored update type: %s", self.name, update_type)

    def _is_duplicate_update(self, key: str) -> bool:
        """Return True if this update/message was already seen."""
        now = time.time()
        if len(self._seen_updates) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_updates = {k: v for k, v in self._seen_updates.items() if v > cutoff}

        if key in self._seen_updates:
            return True
        self._seen_updates[key] = now
        return False

    def _is_self_message(self, message: Message) -> bool:
        """Filter messages sent by the bot itself."""
        if self._bot_user_id is None:
            return False
        sender = getattr(message, "sender", None)
        if sender is None:
            return False
        return getattr(sender, "user_id", None) == self._bot_user_id

    async def _on_message_created(self, event: MessageCreated) -> None:
        """Convert a MAX MessageCreated update into a Hermes MessageEvent."""
        message = event.message
        message_id = message.body.mid if message.body else str(uuid.uuid4().hex)

        if self._is_duplicate_update(f"msg:{message_id}"):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, _safe_id(message_id))
            return

        if self._is_self_message(message):
            logger.debug("[%s] Skipping own message %s", self.name, _safe_id(message_id))
            return

        chat_id = message.recipient.chat_id
        user_id = message.sender.user_id if message.sender else None

        if not self._is_user_allowed(user_id):
            logger.warning(
                "[%s] Dropping message from unauthorized user %s in chat %s",
                self.name,
                _safe_id(user_id),
                _safe_id(chat_id),
            )
            return

        user_name = (
            message.sender.first_name
            if message.sender and getattr(message.sender, "first_name", None)
            else (str(user_id) if user_id else "unknown")
        )
        chat_type = "group" if chat_id is not None and user_id != chat_id else "dm"

        # Normalize IDs to strings for Hermes.
        chat_id_str = str(chat_id) if chat_id is not None else str(user_id)
        user_id_str = str(user_id) if user_id is not None else str(chat_id)

        text = (message.body.text or "").strip() if message.body else ""
        media_urls: List[str] = []
        media_types: List[str] = []

        # Process attachments.
        attachments = (message.body.attachments or []) if message.body else []
        for attachment in attachments:
            try:
                cached = await self._cache_attachment(attachment)
                if cached:
                    media_urls.append(cached.path)
                    media_types.append(cached.media_type)
            except Exception as e:
                logger.warning("[%s] Failed to cache attachment: %s", self.name, e)

        message_type = MessageType.TEXT
        if text.startswith("/"):
            message_type = MessageType.COMMAND
        elif media_types:
            if any(t.startswith("image/") for t in media_types):
                message_type = MessageType.PHOTO
            elif any(t.startswith("video/") for t in media_types):
                message_type = MessageType.VIDEO
            elif any(t.startswith("audio/") for t in media_types):
                message_type = MessageType.VOICE
            elif media_types:
                message_type = MessageType.DOCUMENT

        source = self.build_source(
            chat_id=chat_id_str,
            chat_name=chat_id_str,
            chat_type=chat_type,
            user_id=user_id_str,
            user_name=user_name,
            message_id=message_id,
        )

        reply_to_message_id: Optional[str] = None
        reply_to_text: Optional[str] = None
        linked = getattr(message, "link", None)
        if linked and linked.message:
            reply_to_message_id = linked.message.mid
            reply_to_text = linked.message.text

        timestamp = datetime.fromtimestamp(
            message.timestamp / 1000, tz=timezone.utc
        ) if message.timestamp else datetime.now(timezone.utc)

        msg_event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=message.model_dump() if hasattr(message, "model_dump") else message,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            timestamp=timestamp,
        )

        logger.debug(
            "[%s] Message from %s in %s: %s",
            self.name,
            _safe_id(user_id_str),
            _safe_id(chat_id_str),
            text[:80] if text else "(media)",
        )
        await self._mark_seen(chat_id)
        await self.handle_message(msg_event)

    async def _on_message_callback(self, event: MessageCallback) -> None:
        """Convert a MAX callback into a Hermes command/message event."""
        callback = event.callback
        callback_id = getattr(callback, "callback_id", None)
        if callback_id and self._is_duplicate_update(f"cb:{callback_id}"):
            logger.debug("[%s] Duplicate callback %s, skipping", self.name, _safe_id(callback_id))
            return

        # Acknowledge the callback to MAX.
        try:
            await event.ack()
        except Exception as e:
            logger.debug("[%s] Callback ack failed: %s", self.name, e)

        payload = getattr(callback, "payload", None) or ""
        user = getattr(callback, "user", None)
        user_id = getattr(user, "user_id", None) if user else None

        if not self._is_user_allowed(user_id):
            logger.warning(
                "[%s] Dropping callback from unauthorized user %s",
                self.name,
                _safe_id(user_id),
            )
            return

        message = event.message
        chat_id = message.recipient.chat_id if message else None
        chat_id_str = str(chat_id) if chat_id is not None else str(user_id)
        user_id_str = str(user_id) if user_id is not None else str(chat_id)

        source = self.build_source(
            chat_id=chat_id_str,
            chat_name=chat_id_str,
            chat_type="dm",
            user_id=user_id_str,
            user_name=getattr(user, "first_name", user_id_str) if user else user_id_str,
        )

        # Route callback payloads back to Hermes interactive resolvers.
        text = self._callback_payload_to_command(payload)

        msg_event = MessageEvent(
            text=text or payload,
            message_type=MessageType.TEXT if not text or not text.startswith("/") else MessageType.COMMAND,
            source=source,
            raw_message=event.model_dump() if hasattr(event, "model_dump") else event,
            message_id=callback_id,
        )

        logger.debug(
            "[%s] Callback from %s: payload=%s resolved=%s",
            self.name,
            _safe_id(user_id_str),
            payload,
            text,
        )
        await self._mark_seen(chat_id)
        await self.handle_message(msg_event)

    async def _on_bot_started_or_added(self, event: Any) -> None:
        """Handle the user starting the bot or adding it to a chat."""
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        chat_id_str = str(chat_id)
        source = self.build_source(
            chat_id=chat_id_str,
            chat_name=chat_id_str,
            chat_type="dm",
            user_id=chat_id_str,
            user_name=chat_id_str,
        )
        msg_event = MessageEvent(
            text="/start",
            message_type=MessageType.COMMAND,
            source=source,
            raw_message=event.model_dump() if hasattr(event, "model_dump") else event,
            message_id=str(uuid.uuid4().hex),
        )
        await self._mark_seen(chat_id)
        await self.handle_message(msg_event)

    @staticmethod
    def _callback_payload_to_command(payload: str) -> Optional[str]:
        """Map Hermes inline-button callback payloads to slash commands."""
        if not payload:
            return None
        parts = payload.split(":", 2)
        if len(parts) < 2:
            return None
        prefix, id_or_choice = parts[0], parts[1]

        if prefix == _CLARIFY_PREFIX and len(parts) >= 3:
            # cl:<clarify_id>:<idx>
            return f"/clarify {parts[1]} {parts[2]}"
        if prefix == _APPROVAL_PREFIX:
            # appr:<id>:<choice>
            choice = parts[2] if len(parts) >= 3 else id_or_choice
            if choice == "approve":
                return "/approve"
            if choice == "session":
                return "/approve session"
            if choice in ("always", "permanent"):
                return "/approve always"
            if choice == "deny":
                return "/deny"
            return f"/approve {choice}"
        if prefix == _SLASH_CONFIRM_PREFIX:
            # sc:<choice>:<id>
            choice = id_or_choice
            if choice == "once":
                return "/approve"
            if choice == "always":
                return "/approve always"
            if choice in ("cancel", "deny"):
                return "/deny"
            return f"/approve {choice}"
        return None

    # ------------------------------------------------------------------
    # Media caching
    # ------------------------------------------------------------------

    async def _cache_attachment(self, attachment: Attachment) -> Optional[Any]:
        """Download a MAX attachment and cache it for Hermes vision/tools."""
        url = _resolve_attachment_url(attachment)
        if not url:
            logger.debug("[%s] No downloadable URL for attachment type %s", self.name, type(attachment).__name__)
            return None

        filename = _resolve_attachment_filename(attachment, url)

        # Use maxapi's authenticated session to download the file.
        try:
            data = await self._bot.download_bytes(url)
        except Exception as e:
            logger.warning("[%s] Failed to download attachment %s: %s", self.name, safe_url_for_log(url), e)
            return None

        if not data:
            return None

        kind = "document"
        mime = "application/octet-stream"
        ext = Path(filename).suffix.lower() or ".bin"

        if isinstance(attachment, Image):
            kind = "image"
            mime = "image/jpeg"
            ext = ".jpg" if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif") else ext
            path = cache_image_from_bytes(data, ext=ext)
        elif isinstance(attachment, Video):
            kind = "video"
            mime = "video/mp4"
            ext = ".mp4" if ext not in (".mp4", ".mov", ".avi", ".mkv", ".webm") else ext
            path = cache_video_from_bytes(data, ext=ext)
        elif isinstance(attachment, File):
            kind = "document"
            mime = "application/octet-stream"
            path = cache_document_from_bytes(data, filename)
        else:
            # Audio and other types: classify by content.
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=None)
            return cached

        from tools.credential_files import to_agent_visible_cache_path
        class _SimpleCacheResult:
            def __init__(self, path: str, media_type: str, kind: str, display_name: str):
                self.path = to_agent_visible_cache_path(path)
                self.media_type = media_type
                self.kind = kind
                self.display_name = display_name

            def context_note(self) -> str:
                return f"[{self.kind} '{self.display_name}' saved at: {self.path}]"

        return _SimpleCacheResult(
            path=path,
            media_type=mime,
            kind=kind,
            display_name=filename,
        )

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to MAX."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")

        # Strip MEDIA: tags so they are not sent as literal text.
        content = _strip_media_tag_directives(content)
        if not content:
            return SendResult(success=True)

        parts = self._split_message(content)
        last_message_id: Optional[str] = None

        for i, part in enumerate(parts):
            try:
                link = None
                if reply_to and i == 0:
                    link = NewMessageLink(type="reply", mid=reply_to)
                result = await self._bot.send_message(
                    chat_id=int(chat_id) if chat_id.isdigit() else None,
                    text=part,
                    link=link,
                )
                if result and getattr(result, "message_id", None):
                    last_message_id = str(result.message_id)
            except Exception as e:
                logger.error("[%s] send_message failed: %s", self.name, e)
                error_text = str(e)
                return SendResult(
                    success=False,
                    error=error_text,
                    retryable=isinstance(e, (asyncio.TimeoutError, ConnectionError)),
                )

        return SendResult(success=True, message_id=last_message_id)

    async def _mark_seen(self, chat_id) -> None:
        """Mark a chat\'s messages as read in MAX («прочитано»).

        MAX does not auto-mark incoming messages read just because the bot
        fetched them. Sending SenderAction.MARK_SEEN to the chat turns the
        «прочитано» marker on / clears the unread counter for the user.
        Fire-and-forget: never blocks the event pipeline.
        """
        if not self._bot:
            return
        try:
            await self._bot.send_action(
                chat_id=int(chat_id) if str(chat_id).isdigit() else None,
                action=SenderAction.MARK_SEEN,
            )
        except Exception as e:
            logger.debug("[%s] mark_seen failed: %s", self.name, e)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a single typing indicator to MAX.

        The gateway/base runner calls this repeatedly (its ``_keep_typing``
        background loop refreshes the indicator while the agent works), so a
        one-shot send here is enough.  MAX ``typing_on`` expires client-side
        within a few seconds; the runner refreshes it every ~2s.
        """
        if not self._bot:
            return
        try:
            await self._bot.send_action(
                chat_id=int(chat_id) if str(chat_id).isdigit() else None,
                action=SenderAction.TYPING_ON,
            )
        except Exception as e:
            logger.debug("[%s] send_typing failed: %s", self.name, e)

    async def stop_typing(self, chat_id: str) -> None:
        """Stop typing indication for a chat.

        The base ``_keep_typing`` loop owns the lifecycle (it stops via its
        ``stop_event``/cancellation), so there is nothing to tear down here.
        This hook exists because the gateway runner calls it after delivery.
        """
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image by URL to MAX."""
        return await self._send_media_from_url(
            chat_id=chat_id,
            url=image_url,
            caption=caption,
            reply_to=reply_to,
            media_kind="image",
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file to MAX."""
        return await self._send_media_from_path(
            chat_id=chat_id,
            path=image_path,
            caption=caption,
            reply_to=reply_to,
            media_kind="image",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local document file to MAX."""
        return await self._send_media_from_path(
            chat_id=chat_id,
            path=file_path,
            caption=caption,
            reply_to=reply_to,
            media_kind="document",
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice/audio file to MAX."""
        return await self._send_media_from_path(
            chat_id=chat_id,
            path=audio_path,
            caption=caption,
            reply_to=reply_to,
            media_kind="audio",
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file to MAX."""
        return await self._send_media_from_path(
            chat_id=chat_id,
            path=video_path,
            caption=caption,
            reply_to=reply_to,
            media_kind="video",
        )

    async def _send_media_from_url(
        self,
        chat_id: str,
        url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        media_kind: str,
    ) -> SendResult:
        """Download a remote URL and send it as media."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")
        try:
            from tools.url_safety import is_safe_url
            if not is_safe_url(url):
                return SendResult(success=False, error="Blocked unsafe URL")

            import httpx
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content

            upload_type = {"image": "image", "video": "video", "audio": "audio"}.get(media_kind, "file")
            upload = await self._bot.upload_media(InputMediaBuffer(data, type=upload_type))
            return await self._send_with_attachment(
                chat_id=chat_id,
                text=caption,
                attachment=upload,
                reply_to=reply_to,
            )
        except Exception as e:
            logger.error("[%s] send_media_from_url failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _send_media_from_path(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        media_kind: str,
    ) -> SendResult:
        """Send a local file as media."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")

        safe_path = self.validate_media_delivery_path(path)
        if not safe_path:
            logger.warning("[%s] Refusing to send unsafe media path: %s", self.name, _log_safe_path(path))
            return SendResult(success=False, error="Unsafe media path")

        try:
            upload_type = {"image": "image", "video": "video", "audio": "audio"}.get(media_kind, "file")
            upload = await self._bot.upload_media(InputMedia(safe_path, type=upload_type))
            return await self._send_with_attachment(
                chat_id=chat_id,
                text=caption,
                attachment=upload,
                reply_to=reply_to,
            )
        except Exception as e:
            logger.error("[%s] send_media_from_path failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _send_with_attachment(
        self,
        chat_id: str,
        text: Optional[str],
        attachment: AttachmentUpload,
        reply_to: Optional[str],
    ) -> SendResult:
        """Send a message with a single uploaded attachment."""
        link = None
        if reply_to:
            link = NewMessageLink(type="reply", mid=reply_to)
        try:
            result = await self._bot.send_message(
                chat_id=int(chat_id) if chat_id.isdigit() else None,
                text=text or None,
                attachments=[attachment],
                link=link,
            )
            message_id = str(getattr(result, "message_id", "") or "")
            return SendResult(success=True, message_id=message_id or None)
        except Exception as e:
            logger.error("[%s] send_with_attachment failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Interactive UX
    # ------------------------------------------------------------------

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a clarify question with inline buttons."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")
        if not choices:
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        buttons: List[List[CallbackButton]] = []
        for i, choice in enumerate(choices):
            payload = f"{_CLARIFY_PREFIX}:{clarify_id}:{i}"
            buttons.append([CallbackButton(text=str(choice), payload=payload)])

        # Add an "Other" option for free-text answers.
        buttons.append([CallbackButton(text="Other", payload=f"{_CLARIFY_PREFIX}:{clarify_id}:other")])

        return await self._send_button_message(chat_id, question, buttons, metadata)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: Optional[str] = None,
        approve_text: Optional[str] = None,
        deny_text: Optional[str] = None,
        allow_permanent: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a dangerous-command approval prompt with inline buttons."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")

        approval_id = uuid.uuid4().hex[:12]
        buttons: List[List[CallbackButton]] = [
            [CallbackButton(text=approve_text or "Approve", payload=f"{_APPROVAL_PREFIX}:{approval_id}:approve", intent=Intent.POSITIVE)],
        ]
        if allow_permanent:
            buttons.append([
                CallbackButton(text="Approve always", payload=f"{_APPROVAL_PREFIX}:{approval_id}:always", intent=Intent.POSITIVE),
            ])
        buttons.append([CallbackButton(text=deny_text or "Deny", payload=f"{_APPROVAL_PREFIX}:{approval_id}:deny", intent=Intent.NEGATIVE)])

        text = f"⚠️ **Dangerous command requires approval:**\n```\n{command[:400]}\n```"
        if description:
            text += f"\nReason: {description}"
        return await self._send_button_message(chat_id, text, buttons, metadata)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a slash-command confirmation prompt with inline buttons."""
        if not self._bot:
            return SendResult(success=False, error="Bot not initialized")

        buttons: List[List[CallbackButton]] = [
            [CallbackButton(text="Once", payload=f"{_SLASH_CONFIRM_PREFIX}:once:{confirm_id}")],
            [CallbackButton(text="Always", payload=f"{_SLASH_CONFIRM_PREFIX}:always:{confirm_id}")],
            [CallbackButton(text="Cancel", payload=f"{_SLASH_CONFIRM_PREFIX}:cancel:{confirm_id}", intent=Intent.NEGATIVE)],
        ]
        text = f"**{title}**\n{message}"
        return await self._send_button_message(chat_id, text, buttons, metadata)

    async def _send_button_message(
        self,
        chat_id: str,
        text: str,
        buttons: List[List[CallbackButton]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message with inline callback buttons."""
        attachment = ButtonsPayload(buttons=buttons).pack()
        return await self._send_with_attachment(
            chat_id=chat_id,
            text=text,
            attachment=attachment,
            reply_to=None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _split_message(self, content: str) -> List[str]:
        """Split long content into MAX-safe chunks."""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]

        parts: List[str] = []
        while content:
            if len(content) <= self.MAX_MESSAGE_LENGTH:
                parts.append(content)
                break

            # Prefer splitting on a newline near the limit.
            cut = content.rfind("\n", 0, self.MAX_MESSAGE_LENGTH)
            if cut < self.MAX_MESSAGE_LENGTH // 2:
                cut = self.MAX_MESSAGE_LENGTH

            parts.append(content[:cut])
            content = content[cut:].lstrip()
        return [p for p in parts if p]

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a MAX chat."""
        return {"name": chat_id, "type": "dm"}


# ---------------------------------------------------------------------------
# Standalone sender for cron / send_message_tool when gateway is not running
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process message delivery for cron jobs."""
    if not MAXAPI_AVAILABLE:
        return {"error": "maxapi not installed"}

    token = _get_token(pconfig)
    if not token:
        return {"error": "MAX_BOT_TOKEN not configured"}
    if not chat_id:
        return {"error": "chat_id is required"}

    message = _strip_media_tag_directives(message)

    try:
        bot = Bot(token=token)
        extra = getattr(pconfig, "extra", {}) or {}
        api_url = os.getenv("MAX_API_URL", "").strip() or extra.get("api_url")
        if api_url:
            bot.set_api_url(api_url)

        attachments: List[AttachmentInput] = []
        if media_files:
            for path in media_files:
                safe_path = BasePlatformAdapter.validate_media_delivery_path(path)
                if not safe_path:
                    continue
                try:
                    upload = await bot.upload_media(InputMedia(safe_path))
                    attachments.append(upload)
                except Exception as e:
                    logger.warning("[max standalone] Failed to upload %s: %s", path, e)

        result = await bot.send_message(
            chat_id=int(chat_id) if chat_id.isdigit() else None,
            text=message or None,
            attachments=attachments or None,
        )
        message_id = str(getattr(result, "message_id", "") or "")
        return {"success": True, "platform": "max", "chat_id": chat_id, "message_id": message_id}
    except Exception as e:
        logger.exception("[max standalone] Send failed: %s", e)
        return {"error": f"max standalone send failed: {e}"}


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        return None

    extra: Dict[str, Any] = {"token": token}

    poll_timeout = os.getenv("MAX_POLL_TIMEOUT", "").strip()
    if poll_timeout:
        try:
            extra["poll_timeout"] = int(poll_timeout)
        except ValueError:
            pass

    backoff = os.getenv("MAX_RECONNECT_BACKOFF", "").strip()
    if backoff:
        extra["reconnect_backoff"] = backoff

    api_url = os.getenv("MAX_API_URL", "").strip()
    if api_url:
        extra["api_url"] = api_url

    home = os.getenv("MAX_HOME_CHANNEL", "").strip()
    if home:
        extra["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("MAX_HOME_CHANNEL_NAME", home),
        }

    return extra


def register(ctx) -> None:
    """Plugin entry point called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="max",
        label="MAX",
        adapter_factory=lambda cfg: MaxAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MAX_BOT_TOKEN"],
        install_hint="pip install maxapi  # or install hermes-agent[messaging]",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MAX_ALLOWED_USERS",
        allow_all_env="MAX_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💙",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via MAX Messenger. "
            "MAX supports plain text and basic Markdown-style formatting "
            "(bold, italic, code, links). Keep messages concise; long "
            "responses may be split. Images, videos, documents and voice "
            "messages can be exchanged."
        ),
    )
