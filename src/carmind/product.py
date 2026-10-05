"""Channel-neutral owner experience over the existing CarMind application."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from carmind.assessment import HYPOTHESES, UNCERTAINTIES
from carmind.contracts import SafetyDisposition, UserMessage
from carmind.evidence import FrozenEvidenceSnapshot
from carmind.ownership import CommandType, utc
from carmind.planner_provider import ProviderFailure
from carmind.proactive import NotificationPreferences, notification_text
from carmind.storage import encode


CONFIRM_WORDS = frozenset({"confirm", "yes", "أكد", "تأكيد"})
GREETINGS = frozenset({"سلام", "هلا", "السلام عليكم", "hi", "hello"})
ARABIC_HYPOTHESES = {
    "POSSIBLE_TIRE_LEAK": "قد يكون هناك تسرّب في الإطار، لكن السبب غير مؤكد.",
    "POSSIBLE_STARTING_SYSTEM_ISSUE": "قد تكون المشكلة في البطارية أو نظام التشغيل، ويحتاج الأمر إلى فحص.",
    "POSSIBLE_COOLING_ISSUE": "قد تكون المشكلة في نظام التبريد، لكن السبب غير مؤكد.",
    "POSSIBLE_USAGE_CHANGE": "تغيّر نمط القيادة قد يساهم في ذلك، لكن العلاقة غير مؤكدة.",
    "POSSIBLE_ENGINE_ISSUE": "قد تكون هناك مشكلة مرتبطة بالمحرك، لكن السبب غير مؤكد.",
    "INSUFFICIENT_HISTORY": "قد لا يكون سجل السيارة المكتمل متاحًا لدي.",
}
ARABIC_HYPOTHESIS_BY_TEXT = {HYPOTHESES[key]: value for key, value in ARABIC_HYPOTHESES.items()}
ONBOARDING_PROMPT = """Extract only explicitly stated vehicle identity from this owner message.
Return JSON only: {"type":"vehicle_proposal","owner_quote":string,"vehicle":{
"make":string,"model":string,"year":integer,"odometer":number|null,
"unit":"km"|"mi"|null,"trim":string|null,"engine":string|null,
"market":string|null,"nickname":string|null}}.
If make, model, year, or an explicitly stated odometer unit is missing, return
{"type":"clarification","question":string}. Never infer a market, unit,
manufacturer schedule, or vehicle fact. No database write is authorized."""


def _locale(text, requested):
    if requested in ("ar", "en"):
        return requested
    return "ar" if any("\u0600" <= char <= "\u06ff" for char in text) else "en"


def _asks_for_documentation(text):
    return bool(re.search(
        r"\b(?:owner'?s|vehicle|car|the|my)\s+manual\b(?!\s+transmission)|"
        r"\bmanual\s+(?:says?|recommends?|specifies|lists?)\b|"
        r"\bmanufacturer documentation\b|دليل|كتيب",
        text.casefold()))


def _copy(locale, english, arabic):
    return arabic if locale == "ar" else english


def _distance(value):
    return f"{value:,.0f}" if value is not None else "—"


def _unit(unit, locale):
    return {"km": "كم", "mi": "ميل"}.get(unit, unit) if locale == "ar" else unit


def _date(stamp, locale):
    return stamp.strftime("%d/%m/%Y" if locale == "ar" else "%d %b %Y")


def _label(vehicle):
    p = vehicle.profile
    return vehicle.nickname or f"{p.year} {p.make} {p.model}"


def _service_name(value, locale):
    if locale == "ar":
        return {"oil": "زيت المحرك", "oil_filter": "فلتر الزيت", "tires": "الإطارات",
                "battery": "البطارية", "brakes": "الفرامل", "coolant": "سائل التبريد",
                "air_filter": "فلتر الهواء"}.get(value, value.replace("_", " "))
    return "engine oil" if value == "oil" else value.replace("_", " ")


def _stop_notice(locale, *, continuing=True):
    if continuing:
        return _copy(locale,
                     "The earlier stop warning remains unresolved. Stop when it is safe and get professional help. Do not treat this as permission to keep driving.",
                     "تنبيه التوقف السابق ما زال قائمًا. توقف عندما يكون ذلك آمنًا واطلب مساعدة مختص. هذا ليس إذنًا بمواصلة القيادة.")
    return _copy(locale,
                 "Stop when it is safe and get professional help. Do not treat this as permission to keep driving.",
                 "توقف عندما يكون ذلك آمنًا واطلب مساعدة مختص. هذا ليس إذنًا بمواصلة القيادة.")


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    external_user_id: str
    external_message_id: str
    text: str
    timestamp: datetime
    locale: str | None = None
    confirmation_id: str | None = None
    snapshot: FrozenEvidenceSnapshot | None = None  # trusted offline evidence only
    related_event_id: str | None = None

    def __post_init__(self):
        for value in (self.channel, self.external_user_id, self.external_message_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError("A channel and bounded external identities are required.")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 2000:
            raise ValueError("A nonempty message is required.")
        utc(self.timestamp)
        if self.locale not in (None, "ar", "en"):
            raise ValueError("Unsupported locale.")
        if self.confirmation_id is not None and (not isinstance(self.confirmation_id, str)
                                                 or not self.confirmation_id.strip()
                                                 or len(self.confirmation_id) > 128):
            raise ValueError("Invalid confirmation ID.")
        if self.related_event_id is not None and (not isinstance(self.related_event_id, str)
                                                  or not self.related_event_id.strip()
                                                  or len(self.related_event_id) > 128):
            raise ValueError("Invalid related reminder ID.")


@dataclass(frozen=True)
class ProductReply:
    text: str
    status: str
    active_vehicle_id: str | None = None
    active_vehicle_label: str | None = None
    proposal_id: str | None = None
    safety_notice: str | None = None
    reminders: tuple[dict, ...] = ()
    sources: tuple[dict, ...] = ()
    proposal: dict | None = None


class ProductService:
    """A trusted local service. Adapters authenticate identities before calling it."""

    def __init__(self, app, *, display_timezone="Asia/Riyadh", activity_logger=None):
        self.app = app
        self.store = app.store
        self.display_timezone = ZoneInfo(display_timezone)
        self.activity_logger = activity_logger

    def _manual_index_for(self, vehicle_id):
        configured = self.app.manual_index
        return configured.for_vehicle(vehicle_id) if hasattr(configured, "for_vehicle") else configured

    def notification_preferences(self, channel, external_user_id):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        return asdict(self.app.proactive.preferences(bound["owner_id"]))

    def update_notification_preferences(self, channel, external_user_id, changes, *, now):
        bound = self.store.binding(channel, external_user_id)
        if not bound or not isinstance(changes, dict) or set(changes) - set(NotificationPreferences.__dataclass_fields__):
            raise ValueError("Invalid notification preferences")
        values = {**self.notification_preferences(channel, external_user_id), **changes}
        return asdict(self.app.proactive.set_preferences(bound["owner_id"], NotificationPreferences(**values), now=now))

    def acknowledge_proactive(self, channel, external_user_id, vehicle_id, event_id, *, now):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        self.app.proactive.acknowledge(bound["owner_id"], vehicle_id, event_id, now=now)

    def explain_proactive(self, channel, external_user_id, vehicle_id, event_id, *, now, locale="en"):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        return self.app.proactive.explain(bound["owner_id"], vehicle_id, event_id, now=now,
                                          language=locale)

    def proactive_events(self, channel, external_user_id, vehicle_id, *, now):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        events = self.app.proactive.events(bound["owner_id"], vehicle_id, now=now)
        return [{"id": e.event_id, "type": e.event_type.value, "status": e.status.value,
                 "item": e.maintenance_item, "source_type": e.source_type,
                 "due_km": e.due_km, "due_date": e.due_date.isoformat() if e.due_date else None,
                 "text_en": notification_text(e, "en"), "text_ar": notification_text(e, "ar")}
                for e in events[:10]]

    def manual_documents(self, channel, external_user_id, vehicle_id, *, now):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        self.store.vehicle(bound["owner_id"], vehicle_id, now)
        registry = self.app.manual_index
        return tuple({key: item[key] for key in ("source_id", "document_title", "document_type",
                      "status", "applicability", "page_count")}
                     for item in registry.list(vehicle_id)) if hasattr(registry, "list") else ()

    def add_manual_document(self, channel, external_user_id, vehicle_id, path, *, metadata=None, now):
        from carmind.manufacturer_ingestion import ManufacturerIngestionService, VehicleManualRegistry
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        if not isinstance(self.app.manual_index, VehicleManualRegistry):
            raise ValueError("Local manufacturer onboarding is unavailable")
        return ManufacturerIngestionService(self.store, self.app.manual_index).ingest(
            bound["owner_id"], vehicle_id, path, metadata=metadata, now=now)

    def bind_existing(self, channel, external_user_id, owner_id, session_id):
        """Trusted setup/linking operation; never exposed as a public API route."""
        existing = self.store.binding(channel, external_user_id)
        if existing:
            if (existing["owner_id"], existing["session_id"]) != (owner_id, session_id):
                raise ValueError("External identity already belongs to a different session.")
            return
        with self.store.transaction():
            self.store.bind_channel(channel, external_user_id, owner_id, session_id)

    def _identity(self, channel, external_user_id, now):
        bound = self.store.binding(channel, external_user_id)
        if bound:
            return bound["owner_id"], bound["session_id"]
        owner_id, session_id = str(uuid4()), str(uuid4())
        with self.store.transaction():
            self.store.create_owner(owner_id, now)
            self.store.create_session(session_id, owner_id, now)
            self.store.bind_channel(channel, external_user_id, owner_id, session_id)
        return owner_id, session_id

    @staticmethod
    def _internal_id(inbound):
        source = encode((inbound.channel, inbound.external_user_id, inbound.external_message_id))
        return "channel-" + sha256(source.encode("utf-8")).hexdigest()

    def _vehicle(self, owner_id, session_id, now):
        active = self.store.session(session_id, owner_id)["active_vehicle_id"]
        return self.store.vehicle(owner_id, active, now) if active else None

    def _proposal_copy(self, command, locale):
        args = command.arguments
        kind = command.kind
        correction = (" " + _copy(locale,
            f"This replaces record {args['supersedes_id']}.",
            f"هذا يستبدل السجل {args['supersedes_id']}.") if args.get("supersedes_id") else "")
        if kind == CommandType.UPDATE_ODOMETER:
            stamp = datetime.fromisoformat(args["occurred_at"]).astimezone(self.display_timezone)
            when = stamp.strftime("%d/%m/%Y %H:%M" if locale == "ar" else "%d %b %Y, %I:%M %p")
            return _copy(locale,
                         f"I'll record {_distance(args['reading'])} {args['unit']} on {when}.{correction} Confirm?",
                         f"بسجل قراءة العداد {_distance(args['reading'])} {_unit(args['unit'], locale)} بتاريخ {when}.{correction} تأكد؟")
        if kind == CommandType.RECORD_SERVICE:
            stamp = datetime.fromisoformat(args["performed_at"]).astimezone(self.display_timezone)
            when = stamp.strftime("%d/%m/%Y %H:%M" if locale == "ar" else "%d %b %Y, %I:%M %p")
            service = _service_name(args["service_type"], locale)
            distance = f" at {_distance(args['odometer'])} {args['unit']}" if "odometer" in args else ""
            if locale == "ar":
                distance = f" على {_distance(args['odometer'])} {_unit(args['unit'], locale)}" if "odometer" in args else ""
            note = f" ({args['notes']})" if args.get("notes") else ""
            return _copy(locale,
                         f"I'll record {service} service on {when}{distance}{note}.{correction} Confirm?",
                         f"بسجل صيانة {service} بتاريخ {when}{distance}{note}.{correction} تأكد؟")
        if kind == CommandType.SET_VEHICLE_FIELD:
            return _copy(locale, f"I'll update {args['field']} to {args['value']}. Confirm?",
                         f"بحدّث {args['field']} إلى {args['value']}. تأكد؟")
        if kind == CommandType.SELECT_VEHICLE:
            return _copy(locale, f"I'll switch to vehicle {args['vehicle_id']}. Confirm?",
                         f"بغيّر السيارة إلى {args['vehicle_id']}. تأكد؟")
        return _copy(locale, f"I'll acknowledge reminder {args['reminder_id']}; this does not record completed service. Confirm?",
                     f"بأكد استلام التذكير {args['reminder_id']}؛ هذا لا يسجل إتمام الصيانة. تأكد؟")

    def _draft_copy(self, payload, locale):
        name = f"{payload['year']} {payload['make']} {payload['model']}"
        mileage = payload.get("odometer")
        distance = f" and {_distance(mileage)} {payload['unit']}" if mileage is not None else ""
        if locale == "ar":
            distance = f" وممشاها {_distance(mileage)} {_unit(payload['unit'], locale)}" if mileage is not None else ""
        market = payload.get("market")
        region = f" ({market})" if market else ""
        details = []
        for key, english, arabic in (("trim", "trim", "الفئة"), ("engine", "engine", "المحرك"),
                                     ("nickname", "nickname", "الاسم")):
            if payload.get(key):
                details.append(f"{arabic if locale == 'ar' else english}: {payload[key]}")
        extra = ("; " + "; ".join(details)) if details else ""
        return _copy(locale, f"I'll add your {name}{region}{distance}{extra}. Confirm?",
                     f"بضيف سيارتك {name}{region}{distance}{extra}. تأكد؟")

    def _productize(self, result, owner_id, session_id, now, locale):
        if self.activity_logger is not None and result.trace is not None:
            routing, planner = result.trace.routing, result.trace.planner
            if routing is not None:
                self.activity_logger(f"[ROUTER] mode={routing.router_mode} "
                    f"fallback={routing.fallback_used} loaded={len(routing.effective_loaded_capabilities)}")
            if planner is not None:
                self.activity_logger(f"[PLANNER] calls={planner.planner_call_count} "
                    f"tools={planner.tool_execution_count} status={planner.completion_status}")
                if planner.provider_error is not None:
                    self.activity_logger(f"[PLANNER] provider_error_category={planner.provider_error.category}")
                manual_calls = [entry for entry in planner.tool_executions
                                if entry["tool_id"] == "search_manufacturer_manual"]
                if manual_calls:
                    pages = {page for entry in manual_calls for page in entry.get("manual_pages", [])}
                    self.activity_logger(f"[RAG] queries={len(manual_calls)} pages={len(pages)}")
        vehicle = self._vehicle(owner_id, session_id, now)
        stop = result.safety is not None and result.safety.disposition == SafetyDisposition.STOP_WHEN_SAFE
        continuing = bool(result.trace and result.trace.prior_stop_disposition == SafetyDisposition.STOP_WHEN_SAFE.value)
        safety_notice = _stop_notice(locale, continuing=continuing) if stop else None
        sources = self._manual_sources(result)
        if result.proposed_commands:
            proposal = result.proposed_commands[0]
            text = self._proposal_copy(proposal.command, locale)
            proposal_id = proposal.proposal_id
            proposal_card = {"kind": proposal.command.kind.value,
                             "arguments": proposal.command.arguments}
        elif result.status == "applied":
            text = _copy(locale, "Done. I've saved that change.", "تم، حفظت التغيير.")
            proposal_id = None
            proposal_card = None
        elif result.status == "no_active_vehicle":
            text = _copy(locale, "Tell me your car's make, model and year to get started.",
                         "قل لي شركة سيارتك وموديلها وسنة الصنع عشان نبدأ.")
            proposal_id = None
            proposal_card = None
        else:
            proposal_id = None
            proposal_card = None
            text = self._fact_answer(result, owner_id, vehicle, now, locale, sources)
        if stop and safety_notice not in text:
            text = text.rstrip() + "\n\n" + safety_notice
        return ProductReply(text, result.status, vehicle.profile.vehicle_id if vehicle else None,
                            _label(vehicle) if vehicle else None, proposal_id, safety_notice,
                            sources=sources, proposal=proposal_card)

    @staticmethod
    def _manual_sources(result):
        if result.assessment is None:
            return ()
        cards = {}
        for claim in result.assessment.claims:
            for ref, fact in claim.get("facts", {}).items():
                if fact.get("manual_chunk"):
                    cards[ref] = {"evidence_id": ref, "source_id": fact["source_id"],
                                  "title": fact["document_title"],
                                  "document_type": fact.get("document_type"),
                                  "manufacturer": fact["manufacturer"],
                                  "market": fact.get("source_market"),
                                  "section": fact["section"], "heading": fact["heading"],
                                  "physical_page": fact["physical_page"],
                                  "printed_page": fact["printed_page"],
                                  "applicability": fact["applicability"],
                                  "excerpt": fact["text"][:700]}
        return tuple(cards.values())

    def _fact_answer(self, result, owner_id, vehicle, now, locale, sources=()):
        if vehicle and result.assessment and result.status == "complete":
            claims = result.assessment.assessment
            if sources:
                summary = "\n".join(claims.observations[:2])
                if any(item["applicability"] != "verified_applicable" for item in sources):
                    source = sources[0]
                    title = source["title"]
                    market = source.get("market")
                    context_en = f" ({market} market context)" if market else " (market not established)"
                    context_ar = f" (سياق سوق {market})" if market else " (السوق غير محدد)"
                    caveat = _copy(locale,
                        f"I found this in {title}{context_en}, but its year, market and equipment fit for your car are unverified. Treat it as a reference, not a confirmed specification for your vehicle.",
                        f"لقيت المعلومة في {title}{context_ar}، لكن سنة الدليل ومطابقته لسوق وتجهيزات سيارتك غير مؤكدة. اعتبرها مرجعًا، وليست مواصفة مؤكدة لسيارتك.")
                    return caveat + ("\n\n" + summary if summary else "")
                return summary or _copy(locale, "The source was retrieved, but I can't confirm an answer yet.",
                                        "لقيت المصدر، لكن ما أقدر أؤكد الإجابة بعد.")
            services = {r.record_id: r for r in self.store.service_records(vehicle.profile.vehicle_id, now)}
            cited = [services[key] for key in claims.evidence_ids if key in services]
            if cited and not claims.hypotheses:
                record = cited[0]
                service = _service_name(record.service_type, locale)
                mileage = f" at {_distance(record.odometer_km)} km" if record.odometer_km is not None else ""
                if locale == "ar":
                    mileage = f" على {_distance(record.odometer_km)} كم" if record.odometer_km is not None else ""
                answer = _copy(locale,
                               f"Your last recorded {service} service was on {_date(record.performed_at, 'en')}{mileage}.",
                               f"آخر صيانة {service} مسجلة عندي كانت بتاريخ {_date(record.performed_at, 'ar')}{mileage}.")
                current = vehicle.profile.mileage_km
                if current is not None and record.odometer_km is not None and current >= record.odometer_km:
                    delta = _distance(current - record.odometer_km)
                    answer += _copy(locale, f" You've driven {delta} km since then.",
                                    f" ومشيت {delta} كم من وقتها.")
                return answer
            if vehicle.profile.vehicle_id in claims.evidence_ids and not claims.hypotheses:
                name = _label(vehicle)
                if vehicle.profile.mileage_km is None:
                    answer = _copy(locale, f"I have {name}, but no odometer reading yet.",
                                   f"عندي سيارتك {name}، لكن ما عندي قراءة عداد مسجلة بعد.")
                else:
                    mileage = _distance(vehicle.profile.mileage_km)
                    answer = _copy(locale, f"I have {name} at {mileage} km.",
                                   f"عندي سيارتك {name} وممشاها {mileage} كم.")
                records = self.store.service_records(vehicle.profile.vehicle_id, now, limit=1)
                if records:
                    service = records[0]
                    answer += _copy(locale,
                        f" Last recorded service: {_service_name(service.service_type, 'en')} at {service.odometer_km:,.0f} km." if service.odometer_km is not None else
                        f" Last recorded service: {_service_name(service.service_type, 'en')}.",
                        f" آخر صيانة مسجلة: {_service_name(service.service_type, 'ar')} على {service.odometer_km:,.0f} كم." if service.odometer_km is not None else
                        f" آخر صيانة مسجلة: {_service_name(service.service_type, 'ar')}.")
                return answer
            if claims.observations:
                answer = "\n".join(claims.observations[:2])
                if claims.hypotheses:
                    hypothesis = claims.hypotheses[0]
                    if locale == "ar":
                        hypothesis = ARABIC_HYPOTHESIS_BY_TEXT.get(hypothesis, hypothesis)
                    answer += _copy(locale, "\nPossible, not confirmed: ", "\nاحتمال غير مؤكد: ") + hypothesis
                    if result.safety.disposition == SafetyDisposition.UNDETERMINED:
                        answer += _copy(locale,
                                        "\nI can't determine whether driving is safe from this information.",
                                        "\nما أقدر أحدد من هذه المعلومات إذا كانت القيادة آمنة.")
                return answer
            if UNCERTAINTIES["APPLICABILITY_UNVERIFIED"] in claims.uncertainties:
                return _copy(locale,
                             "I have your vehicle and service records, but no verified maintenance schedule for this exact model and market. I won't guess the next interval.",
                             "عندي بيانات سيارتك وسجل صيانتها، لكن ما عندي جدول صيانة موثّق ومطابق لموديلها وسوقها. ما راح أخمّن الموعد القادم.")
            if result.safety.disposition == SafetyDisposition.UNDETERMINED:
                return _copy(locale,
                             "I don't have enough current evidence to say whether driving is safe. Please have the concern checked if it continues.",
                             "ما عندي معلومات حالية كافية عشان أقول إن القيادة آمنة. إذا استمرت المشكلة، خلّ مختص يفحصها.")
            if result.safety.disposition == SafetyDisposition.STOP_WHEN_SAFE:
                return _copy(locale, "I don't have a complete explanation yet.",
                             "ما عندي تفسير كامل للمشكلة حتى الآن.")
        if result.status == "incomplete":
            reason = result.trace.planner.stop_reason if result.trace and result.trace.planner else None
            if reason in {"provider_failure", "provider_timeout", "provider_connection_failure"}:
                return _copy(locale, "Conversation is unavailable right now. Your saved car information is still here.",
                             "المحادثة غير متاحة حاليًا. معلومات سيارتك المحفوظة ما زالت موجودة.")
            if reason in {"call_budget_exhausted", "tool_budget_exhausted"}:
                return _copy(locale, "I need another turn to finish that answer. Your saved car information is unchanged.",
                             "أحتاج رسالة أخرى عشان أكمل الإجابة. معلومات سيارتك المحفوظة ما تغيرت.")
            return _copy(locale, "I couldn't verify an answer yet. Could you rephrase your question?",
                         "ما قدرت أتحقق من الإجابة بعد. ممكن تعيد صياغة سؤالك؟")
        if result.status == "clarification_required":
            question = result.response.removesuffix("\nNo car record was changed.")
            return question
        if result.status in {"rejected", "error"}:
            return _copy(locale, "I couldn't save that change. Your recorded car information is unchanged.",
                         "ما قدرت أحفظ هذا التغيير. معلومات سيارتك المسجلة ما تغيرت.")
        return result.response

    def _onboard(self, owner_id, session_id, message, now, locale):
        prior = self.store.vehicle_draft_for_message(session_id, message.message_id)
        if prior:
            proposal_id, data = prior["id"], json.loads(prior["payload"])
        else:
            try:
                raw = json.loads(self.app.provider.generate([
                    {"role": "system", "content": ONBOARDING_PROMPT},
                    {"role": "user", "content": message.text},
                ]).text)
                if not isinstance(raw, dict):
                    raise ValueError("Invalid vehicle proposal")
                if raw.get("type") == "clarification":
                    question = raw.get("question")
                    if isinstance(question, str) and 0 < len(question) <= 240:
                        return ProductReply(question, "clarification_required")
                if (set(raw) != {"type", "owner_quote", "vehicle"} or raw["type"] != "vehicle_proposal"
                        or not isinstance(raw["owner_quote"], str) or not raw["owner_quote"].strip()
                        or raw["owner_quote"] not in message.text):
                    raise ValueError("Invalid vehicle proposal")
                data = raw["vehicle"]
                allowed = {"make", "model", "year", "odometer", "unit", "trim", "engine", "market", "nickname"}
                if not isinstance(data, dict) or set(data) - allowed or not {"make", "model", "year"} <= data.keys():
                    raise ValueError("Invalid vehicle fields")
                proposal_id, data = self.app.propose_vehicle(owner_id, session_id, message, now=now, **data)
            except ProviderFailure as failure:
                if self.activity_logger is not None:
                    self.activity_logger(f"[PLANNER] provider_error_category={failure.diagnostic.category}")
                return ProductReply(_copy(locale,
                    "Conversation is unavailable right now. Your saved car information is still here.",
                    "المحادثة غير متاحة حاليًا. معلومات سيارتك المحفوظة ما زالت موجودة."),
                    "unavailable")
            except (ValueError, TypeError, KeyError, StopIteration, RuntimeError):
                return ProductReply(_copy(locale,
                    "Tell me your car's make, model and year. Include the odometer with its unit if you know it.",
                    "قل لي شركة السيارة وموديلها وسنة الصنع. وإذا تعرف الممشى، اذكره مع وحدته."),
                    "clarification_required")
        return ProductReply(self._draft_copy(data, locale), "confirmation_required",
                            proposal_id=proposal_id,
                            proposal={"kind": "add_vehicle", "arguments": data})

    def handle(self, inbound: InboundMessage) -> ProductReply:
        now = utc(inbound.timestamp)
        locale = _locale(inbound.text, inbound.locale)
        owner_id, session_id = self._identity(inbound.channel, inbound.external_user_id, now)
        previous = self.store.receipt(inbound.channel, inbound.external_user_id, inbound.external_message_id)
        if previous is not None:
            previous["reminders"] = tuple(previous.get("reminders", ()))
            previous["sources"] = tuple(previous.get("sources", ()))
            return ProductReply(**previous)
        message = UserMessage(self._internal_id(inbound), inbound.text, now)
        proposal_id = inbound.confirmation_id
        if proposal_id is None and inbound.text.strip().casefold() in CONFIRM_WORDS:
            pending = self.store.pending_for_session(session_id, now)
            if len(pending) != 1:
                reply = ProductReply(_copy(locale,
                    "Please confirm the exact change shown above. If more than one is pending, select the one you mean.",
                    "أكد التغيير المعروض بالضبط. إذا عندك أكثر من تغيير معلّق، اختر المطلوب."),
                    "clarification_required")
                self._save_reply(inbound, reply)
                return reply
            proposal_id = pending[0]
        if proposal_id and proposal_id.startswith("vehicle-"):
            try:
                vehicle_id, applied = self.app.confirm_vehicle(owner_id, session_id, proposal_id, message, now=now)
                vehicle = self.store.vehicle(owner_id, vehicle_id, now)
                reply = ProductReply(_copy(locale, f"Done. I've saved {_label(vehicle)} as your car.",
                                            f"تم، حفظت {_label(vehicle)} كسيارتك."),
                                     "applied" if applied else "replayed", vehicle_id, _label(vehicle))
            except ValueError:
                reply = ProductReply(_copy(locale, "That vehicle confirmation is no longer available.",
                                           "تأكيد السيارة هذا ما عاد متاح."), "error")
        elif proposal_id:
            result = self.app.handle_message(owner_id, session_id, message, now=now,
                                             confirmation_id=proposal_id)
            reply = self._productize(result, owner_id, session_id, now, locale)
        else:
            vehicle = self._vehicle(owner_id, session_id, now)
            related_event_id = inbound.related_event_id
            if related_event_id is None and inbound.channel == "whatsapp" and vehicle is not None:
                related_event_id = self.store.latest_sent_event_id(owner_id, vehicle.profile.vehicle_id,
                                                                     now - timedelta(days=7))
            if inbound.text.strip().casefold().rstrip("!؟?.") in GREETINGS:
                if vehicle:
                    greeting = _copy(locale, f"Hi! I have {_label(vehicle)} as your car. What would you like to know?",
                                     f"هلا! سيارتك الحالية عندي {_label(vehicle)}. وش حاب تعرف عنها؟")
                else:
                    greeting = _copy(locale, "Hi! I can help with your car, maintenance, and questions. What's your car?",
                                     "هلا! أقدر أساعدك تتابع سيارتك وصيانتها ومشاكلها. وش سيارتك؟")
                stop = self.app.unresolved_safety(owner_id, vehicle.profile.vehicle_id, now=now) if vehicle else None
                notice = _stop_notice(locale) if stop else None
                reply = ProductReply(greeting + ("\n\n" + notice if notice else ""), "complete",
                                     vehicle.profile.vehicle_id if vehicle else None,
                                     _label(vehicle) if vehicle else None, safety_notice=notice)
            elif vehicle is None:
                reply = self._onboard(owner_id, session_id, message, now, locale)
            elif related_event_id and inbound.text.strip().casefold().rstrip("!؟?.") in {
                    "why this reminder", "why are you reminding me", "ليش التذكير", "ليش تذكرني"}:
                try:
                    explanation = self.app.proactive.explain(owner_id, vehicle.profile.vehicle_id,
                                                               related_event_id, now=now, language=locale)
                except ValueError:
                    explanation = _copy(locale, "That reminder is no longer available.", "التذكير هذا ما عاد متاح.")
                stop = self.app.unresolved_safety(owner_id, vehicle.profile.vehicle_id, now=now)
                notice = _stop_notice(locale) if stop else None
                reply = ProductReply(explanation + ("\n\n" + notice if notice else ""), "complete",
                                     vehicle.profile.vehicle_id, _label(vehicle), safety_notice=notice)
            elif self._manual_index_for(vehicle.profile.vehicle_id) is None and _asks_for_documentation(inbound.text):
                text = _copy(locale,
                    "I don't have manufacturer documentation configured for your car. I can still help with your symptoms and saved car history, but I won't guess a manual specification.",
                    "ما عندي دليل الشركة المصنعة مضاف لسيارتك. أقدر أساعدك بالأعراض وسجل سيارتك، لكن ما راح أخمّن مواصفة من الدليل.")
                stop = self.app.unresolved_safety(owner_id, vehicle.profile.vehicle_id, now=now)
                notice = _stop_notice(locale) if stop else None
                reply = ProductReply(text + ("\n\n" + notice if notice else ""), "unavailable",
                                     vehicle.profile.vehicle_id, _label(vehicle), safety_notice=notice)
            else:
                result = self.app.handle_message(owner_id, session_id, message, now=now,
                                                 snapshot=inbound.snapshot, related_event_id=related_event_id)
                reply = self._productize(result, owner_id, session_id, now, locale)
        self._save_reply(inbound, reply)
        return reply

    def _save_reply(self, inbound, reply):
        with self.store.transaction():
            self.store.save_receipt(inbound.channel, inbound.external_user_id,
                                    inbound.external_message_id, asdict(reply))

    def overview(self, channel, external_user_id, *, now, vehicle_id=None):
        """Deterministic projection. It makes zero router or planner calls."""
        now = utc(now)
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity.")
        owner_id, session_id = bound["owner_id"], bound["session_id"]
        session = self.store.session(session_id, owner_id)
        target = vehicle_id or session["active_vehicle_id"]
        if target is None:
            return {"vehicle": None,
                    "vehicles": [{"id": v.profile.vehicle_id, "label": _label(v)}
                                 for v in self.store.vehicles(owner_id, now)],
                    "manufacturer": {"status": "unverified"}, "documents": [],
                    "needs_attention": [], "notification_preferences": self.notification_preferences(channel, external_user_id)}
        vehicle = self.store.vehicle(owner_id, target, now)
        events = self.store.odometer_events(target)
        latest_event = max((e for e in events if datetime.fromisoformat(e["occurred_at"]) <= now),
                           key=lambda e: (e["occurred_at"], e["created_at"], e["id"]), default=None)
        services = self.store.service_records(target, now, limit=10)
        last = services[0] if services else None
        request, state = self.app.read_maintenance(owner_id, target, now=now)
        reminders = self.store.reminders(target)
        stop = self.app.unresolved_safety(owner_id, target, now=now)
        recent_concern = None
        for turn in reversed(self.store.recent_turns(session_id, target)):
            if turn["summary"]:
                summary = json.loads(turn["summary"])
                if summary.get("unconfirmed_hypotheses"):
                    recent_concern = {"observations": summary.get("observations", [])[:2],
                                      "hypotheses": summary["unconfirmed_hypotheses"][:2]}
                    break
        current = vehicle.profile.mileage_km
        gap = current - last.odometer_km if last and current is not None and last.odometer_km is not None and current >= last.odometer_km else None
        return {
            "vehicle": {"id": target, "label": _label(vehicle), "make": vehicle.profile.make,
                        "model": vehicle.profile.model, "year": vehicle.profile.year,
                        "trim": vehicle.trim, "engine": vehicle.profile.engine,
                        "market": vehicle.market, "nickname": vehicle.nickname},
            "vehicles": [{"id": v.profile.vehicle_id, "label": _label(v)} for v in self.store.vehicles(owner_id, now)],
            "odometer": {"km": current, "recorded_at": latest_event["occurred_at"] if latest_event else None,
                         "original_reading": latest_event["reading"] if latest_event else None,
                         "original_unit": latest_event["unit"] if latest_event else None},
            "last_service": {"type": last.service_type, "at": last.performed_at.isoformat(),
                             "odometer_km": last.odometer_km, "km_since": gap} if last else None,
            "services": [{"id": r.record_id, "type": r.service_type, "at": r.performed_at.isoformat(),
                          "odometer_km": r.odometer_km} for r in services[:10]],
            "maintenance": [{"item": r.maintenance_item, "status": r.status,
                             "due_odometer_km": r.due_odometer_km, "due_date": r.due_date.isoformat() if r.due_date else None,
                             "source_id": r.source_id} for r in state if r.status in {"UPCOMING", "DUE", "OVERDUE"}],
            "reminders": [{"id": r["id"], "item": r["facts"]["maintenance_item"],
                           "status": r["facts"]["status"], "lifecycle": r["lifecycle"]} for r in reminders[:10]],
            "active_concern": {"unresolved_stop": True, "notice": _stop_notice("en")} if stop else None,
            "recent_concern": recent_concern,
            "manufacturer": {"status": ("test_only" if request.pack.poc_only else "verified") if request else "unverified",
                             "sources": [{"id": s.source_id, "title": s.document_title,
                                          "market": s.market} for s in request.pack.sources] if request else []},
            "documents": list(self.manual_documents(channel, external_user_id, target, now=now)),
            "needs_attention": self.proactive_events(channel, external_user_id, target, now=now),
            "notification_preferences": self.notification_preferences(channel, external_user_id),
        }

    def select_vehicle(self, channel, external_user_id, vehicle_id, *, now):
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity.")
        self.app.select_vehicle(bound["owner_id"], bound["session_id"], vehicle_id, now=now)

    def cancel_proposal(self, channel, external_user_id, proposal_id):
        if (not isinstance(proposal_id, str) or len(proposal_id) > 128 or
                not proposal_id.startswith(("vehicle-", "command-"))):
            raise ValueError("Invalid proposal ID")
        bound = self.store.binding(channel, external_user_id)
        if not bound:
            raise ValueError("Unknown product identity")
        with self.store.transaction():
            if not self.store.cancel_pending_proposal(bound["session_id"], proposal_id):
                raise ValueError("Proposal is not pending in this session")
