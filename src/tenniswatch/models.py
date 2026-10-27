"""Relational schema for facilities, policies, observations and notification outbox."""
from __future__ import annotations
from datetime import date, datetime, time
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint, Index, func, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from .database import Base

JSONDocument = JSON().with_variant(JSONB, "postgresql")


class ReservationSite(Base):
    __tablename__ = "reservation_sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_minutes: Mapped[int] = mapped_column(Integer, default=60)


class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("reservation_sites.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    booking_url: Mapped[str] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    collection_status: Mapped[str] = mapped_column(String(24), default="UNVERIFIED", server_default="UNVERIFIED")
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("site_id", "external_id"),)


class Court(Base):
    __tablename__ = "courts"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    __table_args__ = (UniqueConstraint("facility_id", "external_id"),)


class ReservationPolicy(Base):
    __tablename__ = "reservation_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    audience: Mapped[str] = mapped_column(String(30))  # resident/general
    open_days_before: Mapped[int] = mapped_column(Integer)
    rule_type: Mapped[str] = mapped_column(String(24), default="rolling_days", server_default="rolling_days")
    open_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_time: Mapped[time] = mapped_column(Time(timezone=True))
    timezone_name: Mapped[str] = mapped_column(String(64), default="Asia/Seoul")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_check_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    __table_args__ = (CheckConstraint(
        "(rule_type = 'rolling_days' AND open_days_before BETWEEN 0 AND 365) OR "
        "(rule_type = 'weekly_batch' AND open_days_before = 0 AND open_weekday IS NOT NULL "
        "AND open_weekday BETWEEN 1 AND 7 AND window_start_days IS NOT NULL "
        "AND window_start_days BETWEEN 1 AND 7 AND window_days IS NOT NULL AND window_days = 7)", name="ck_policy_rule"),)


class PolicyChange(Base):
    __tablename__ = "policy_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("reservation_policies.id"))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReservationSchedule(Base):
    __tablename__ = "reservation_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("reservation_policies.id"))
    play_date: Mapped[date] = mapped_column(Date)
    play_date_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    audience: Mapped[str] = mapped_column(String(30))
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("facility_id", "play_date", "audience"),)


class PolicyCheckRun(Base):
    __tablename__ = "policy_check_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(100), unique=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    weekdays: Mapped[str | None] = mapped_column(String(30))
    start_time: Mapped[time | None] = mapped_column(Time(timezone=True))
    end_time: Mapped[time | None] = mapped_column(Time(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("user_id", "facility_id"),)


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'processed', 'failed')"),
        CheckConstraint("attempts >= 0"),
        CheckConstraint("length(message_text) BETWEEN 1 AND 4096"),
    )


class UserWatch(Base):
    __tablename__ = "user_watches"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id"))
    watch_type: Mapped[str] = mapped_column(String(24))
    criteria: Mapped[dict] = mapped_column(JSONDocument)
    criteria_hash: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    monitoring_status: Mapped[str] = mapped_column(String(32), default="PENDING_ACTIVATION", server_default="PENDING_ACTIVATION")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "watch_type", "criteria_hash"),)


class UserWatchFacility(Base):
    __tablename__ = "user_watch_facilities"
    user_watch_id: Mapped[int] = mapped_column(ForeignKey("user_watches.id", ondelete="CASCADE"), primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), primary_key=True)


class ConversationContext(Base):
    __tablename__ = "conversation_contexts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"))
    conversation_id: Mapped[str] = mapped_column(String(100))
    last_intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_search_filters: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    active_subscription_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user_watches.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("user_id", "conversation_id"),)


class AgentToolInvocation(Base):
    __tablename__ = "agent_tool_invocations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    update_id: Mapped[int] = mapped_column(ForeignKey("telegram_updates.update_id", ondelete="CASCADE"))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    tool_name: Mapped[str] = mapped_column(String(80))
    args_hash: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    result: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("update_id", "tool_call_id", name="uq_tool_invocation_update_call"),
        UniqueConstraint("update_id", "tool_name", "args_hash", name="uq_tool_invocation_update_action_args"),
    )


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    update_id: Mapped[int] = mapped_column(ForeignKey("telegram_updates.update_id", ondelete="CASCADE"))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    request_key: Mapped[str] = mapped_column(String(36), unique=True)
    model_name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), server_default="RESERVED")
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyReportSetting(Base):
    __tablename__ = "daily_report_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True)
    minute_of_day: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("minute_of_day >= 0 AND minute_of_day < 1440"),)


class AvailabilitySlot(Base):
    __tablename__ = "availability_slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    court_id: Mapped[int | None] = mapped_column(ForeignKey("courts.id"), nullable=True)
    slot_key: Mapped[str] = mapped_column(String(400))
    court_name: Mapped[str] = mapped_column(String(200))
    play_date: Mapped[date] = mapped_column(Date)
    starts_at: Mapped[time] = mapped_column(Time(timezone=True))
    ends_at: Mapped[time] = mapped_column(Time(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    booking_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("facility_id", "slot_key"),)


class AvailabilityEvent(Base):
    __tablename__ = "availability_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("availability_slots.id"))
    event_key: Mapped[str] = mapped_column(String(500), unique=True)
    previous_status: Mapped[str] = mapped_column(String(20))
    current_status: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("availability_events.id"), nullable=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("reservation_schedules.id"), nullable=True)
    schedule_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id"))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "report_date", name="uq_notification_daily_user_date"),
        UniqueConstraint("event_id", "user_id", "kind", name="uq_notification_event_user_kind"),
        UniqueConstraint("schedule_id", "user_id", "kind", "schedule_opens_at", name="uq_notification_schedule_user_kind"),
    )


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("reservation_sites.id"))
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), nullable=True)
    requested_dates: Mapped[list | None] = mapped_column(JSONDocument, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    records_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class SchedulerJob(Base):
    __tablename__ = 'scheduler_jobs'
    job_key: Mapped[str] = mapped_column(Text, primary_key=True)
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default='PENDING')
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict | None] = mapped_column(JSONDocument)
    alert_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
