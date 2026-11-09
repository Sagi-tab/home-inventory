"""Channel-independent chat handling shared by the WhatsApp and Telegram bridges.

A bridge turns its transport's messages into calls on :class:`ChatInbound`
(text, a shared location, a receipt to read) and supplies the transport-specific
pieces through a few hooks: who may talk, who they are, and how to reply.

Everything else is the same for every channel: pending receipts get first
refusal on a reply, a Google Maps link answers a reminder waiting for its
place, and the rest goes to the conversation agent, run as the sender's Home
Assistant user when the sender is mapped to a person.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_RECEIPTS_ENABLED, CONF_WA_AGENT_ID, CONF_WA_DEBUG, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Downloads an attachment: returns (bytes, mime type) or raises ReceiptError.
MediaFetcher = Callable[[], Awaitable[tuple[bytes, str]]]


class ChatInbound:
    """Shared message flow. Subclasses implement the hooks below."""

    #: Short channel name, used in logs and diagnostics counters.
    channel = "chat"
    #: hass.data key holding this channel's activity counters.
    stats_key = "chat_stats"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    # ---------- hooks ----------

    @property
    def _options(self) -> dict:
        """The integration's options (shared by all channels)."""
        return self.hass.data.get(DOMAIN, {}).get("wa_options", {})

    def _sender_allowed(self, sender: str) -> bool:
        raise NotImplementedError

    def _person_for(self, sender: str) -> str | None:
        """The ``person.*`` entity the sender is mapped to, if any."""
        raise NotImplementedError

    def _user_for(self, sender: str) -> str | None:
        """The Home Assistant user id of the sender's person, if any."""
        raise NotImplementedError

    def _conversation_id(self, sender: str) -> str:
        raise NotImplementedError

    def _mask(self, sender: str | None) -> str:
        """The sender as it may appear in logs."""
        return str(sender)

    async def _async_reply(self, to: str, body: str) -> None:
        raise NotImplementedError

    # ---------- shared helpers ----------

    @property
    def _reminders(self):
        return self.hass.data.get(DOMAIN, {}).get("reminders")

    def _record(self, event: str, detail: str | None = None) -> None:
        """Count activity so it can be inspected without logs (diagnostics)."""
        stats = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self.stats_key, {"counts": {}, "last_event": None, "last_event_at": None}
        )
        stats["counts"][event] = stats["counts"].get(event, 0) + 1
        stats["last_event"] = f"{event}: {detail}" if detail else event
        stats["last_event_at"] = dt_util.utcnow().isoformat(timespec="seconds")

    def _trace(self, msg: str, *args: Any) -> None:
        """Log the message flow; info level while the debug option is on."""
        if self._options.get(CONF_WA_DEBUG):
            _LOGGER.info(f"[{self.channel}] " + msg, *args)
        else:
            _LOGGER.debug(f"[{self.channel}] " + msg, *args)

    # ---------- entry points for the bridges ----------

    async def async_handle_location(
        self, sender: str, lat: float, lon: float, label: str = "", body: str = ""
    ) -> None:
        """A shared location completes a reminder waiting for one; otherwise
        the agent gets it as text to act on."""
        reminders = self._reminders
        person = self._person_for(sender) if reminders else None
        self._trace("location shared by %s", self._mask(sender))
        if person and (
            reply := await reminders.async_attach_shared_location(person, lat, lon)
        ):
            await self._async_reply(sender, reply)
            return
        text = f"[shared location: {lat:.6f}, {lon:.6f}]" + (f" {label}" if label else "")
        # Some engines put the map thumbnail in the body; only pass real text.
        body = str(body or "").strip()
        if body and len(body) < 500:
            text += f"\n{body}"
        answer = await self._async_converse(text, sender)
        if answer:
            await self._async_reply(sender, answer)

    async def async_handle_text(self, sender: str, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            self._trace("empty message body, nothing to do")
            return

        # A pending receipt gets first refusal on the reply: "yes" means confirm
        # the receipt, not something for the agent to interpret. Anything it
        # does not consume falls through to normal conversation.
        if await self._async_receipt_reply(sender, text):
            return

        # A Google Maps link answers a reminder that is waiting for its place.
        reminders = self._reminders
        person = self._person_for(sender) if reminders else None
        if person and (reply := await reminders.async_attach_maps_link(person, text)):
            await self._async_reply(sender, reply)
            return

        self._trace("asking agent %r: %r",
                    self._options.get(CONF_WA_AGENT_ID) or "<default>", text[:200])
        answer = await self._async_converse(text, sender)
        self._trace("agent answered: %r", (answer or "")[:200])
        if answer:
            await self._async_reply(sender, answer)
        else:
            _LOGGER.warning(
                "Conversation agent returned no reply for %s; nothing sent",
                self._mask(sender),
            )

    async def async_receipts_enabled(self, sender: str) -> bool:
        """False (after telling the sender) when receipt scanning is off."""
        if not self._options.get(CONF_RECEIPTS_ENABLED, True):
            await self._async_reply(sender, "Receipt scanning is turned off.")
            return False
        return True

    async def async_read_receipt(
        self, sender: str, mime: str, fetch: MediaFetcher
    ) -> None:
        """Download a photo/PDF, extract the items, and offer them for confirm."""
        from . import receipts

        self._record("receipt_received", mime)
        self._trace("receipt %s from %s", mime, self._mask(sender))
        await self._async_reply(sender, "Got it - reading the receipt...")

        path = None
        try:
            data, mime = await fetch()
            path, content_id = await receipts.async_stage_media(self.hass, data, mime)
            items = await receipts.async_extract_items(self.hass, content_id, mime)
            if not items:
                self._record("receipt_empty")
                await self._async_reply(
                    sender,
                    "I couldn't find any products on that receipt. Try a "
                    "photo taken straight on, in good light, close enough "
                    "that the item lines are readable - and one photo per "
                    "part if the receipt is long.",
                )
                return

            lines = await receipts.async_resolve_lines(self.hass, items)
            receipts.session_start(self.hass, sender, lines)
            self._record("receipt_extracted", f"{len(lines)} lines")
            await self._async_reply(sender, receipts.format_preview(lines))
        except receipts.ReceiptError as err:
            self._record("receipt_failed", str(err)[:80])
            _LOGGER.error("Receipt processing failed: %s", err)
            await self._async_reply(sender, str(err))
        except Exception as err:  # noqa: BLE001
            self._record("receipt_failed", type(err).__name__)
            _LOGGER.exception("Receipt processing crashed")
            await self._async_reply(sender, f"Something went wrong: {err}")
        finally:
            if path is not None:
                await receipts.async_cleanup(self.hass, path)

    # ---------- receipts ----------

    async def _async_receipt_reply(self, sender: str, text: str) -> bool:
        """Handle a reply belonging to a pending receipt.

        Returns True when the message was consumed, so the caller knows not to
        pass it on to the conversation agent.
        """
        from . import receipts

        session = receipts.session_get(self.hass, sender)
        if not session:
            return False

        answer = text.strip().lower()

        if session["state"] == "awaiting_confirm":
            if answer in receipts.CANCEL_WORDS:
                receipts.session_clear(self.hass, sender)
                await self._async_reply(sender, "Discarded - nothing was added.")
                return True
            # "drop 3" before the confirm check: a misread line should cost
            # that line, not the whole receipt.
            drop = receipts.parse_drop_request(text, len(session["lines"]))
            if drop is not None:
                await self._async_drop_lines(sender, session, drop)
                return True

            if answer not in receipts.CONFIRM_WORDS:
                # Not an answer to us; let the agent field it and keep waiting.
                return False

            lines = session["lines"]
            perishables = [
                (i, line) for i, line in enumerate(lines) if receipts.is_perishable(line)
            ]
            if perishables:
                session["state"] = "awaiting_dates"
                session["pending_dates"] = perishables
                await self._async_reply(sender, receipts.format_date_prompt(perishables))
                return True
            await self._async_finish_receipt(sender, session)
            return True

        if session["state"] == "awaiting_dates":
            perishables = session["pending_dates"]
            if answer not in receipts.SKIP_WORDS:
                try:
                    dates = await receipts.async_parse_dates(self.hass, text, perishables)
                except receipts.ReceiptError as err:
                    await self._async_reply(sender, f"{err} Adding without dates.")
                    dates = {}
                for number, iso in dates.items():
                    line_index = perishables[number - 1][0]
                    session["lines"][line_index]["expiration_date"] = iso
            await self._async_finish_receipt(sender, session)
            return True

        return False

    async def _async_drop_lines(self, sender: str, session: dict, drop: list[int]) -> None:
        """Remove the named lines and re-show the receipt for confirmation."""
        from . import receipts

        if not drop:
            await self._async_reply(
                sender,
                f"That receipt only has {len(session['lines'])} line(s) - "
                "reply with a number from the list, or YES to add them all.",
            )
            return

        removed = set(drop)
        session["lines"] = [
            line for index, line in enumerate(session["lines"]) if index not in removed
        ]
        self._record("receipt_lines_dropped", str(len(removed)))
        if not session["lines"]:
            receipts.session_clear(self.hass, sender)
            await self._async_reply(sender, "That was every line - nothing was added.")
            return
        await self._async_reply(
            sender,
            f"Dropped {len(removed)} line(s).\n\n" + receipts.format_preview(session["lines"]),
        )

    async def _async_finish_receipt(self, sender: str, session: dict) -> None:
        from . import receipts

        try:
            result = await receipts.async_commit(self.hass, session["lines"])
        except receipts.ReceiptError as err:
            self._record("receipt_failed", str(err)[:80])
            await self._async_reply(sender, str(err))
            return
        finally:
            receipts.session_clear(self.hass, sender)

        self._record("receipt_committed", f"{result['count']} items")
        summary = [f"Added {result['count']} item(s) to the inventory."]
        if result["products_created"]:
            summary.append(f"{result['products_created']} new product(s) created.")
        if result["fulfilled_shopping_ids"]:
            summary.append(
                f"{len(result['fulfilled_shopping_ids'])} shopping list "
                "item(s) marked as bought."
            )
        await self._async_reply(sender, " ".join(summary))

    # ---------- conversation ----------

    async def _async_converse(self, text: str, sender: str) -> str | None:
        """Hand the text to the configured conversation agent."""
        agent_id = self._options.get(CONF_WA_AGENT_ID)
        # Run as the sender's Home Assistant user (when mapped in the options),
        # so tools such as reminders know who is asking. One conversation per
        # sender keeps follow-ups ("make it 3 instead") in context.
        user_id = self._user_for(sender) if self._reminders else None
        try:
            result = await conversation.async_converse(
                self.hass,
                text=text,
                conversation_id=self._conversation_id(sender),
                context=Context(user_id=user_id) if user_id else None,
                language=self.hass.config.language,
                agent_id=agent_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Conversation agent failed")
            return f"Sorry, I couldn't process that ({err})."

        try:
            return result.response.speech["plain"]["speech"]
        except (AttributeError, KeyError, TypeError):
            return "Done."
