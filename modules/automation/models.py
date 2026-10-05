"""Automation module tables (mod_automation_*)."""

from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class AutomationWorkflow(db.Model):
    """A user-owned workflow: a DAG of nodes plus its schedule trigger."""

    __tablename__ = "mod_automation_workflow"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, default="Untitled workflow")
    description = db.Column(db.Text, nullable=False, default="")
    # JSON graph: {"nodes": [{id, type, x, y, config}], "edges": [{from,
    # from_port, to, to_port}]} — the canonical form; the editor converts
    # to/from the canvas library's own format.
    graph = db.Column(db.Text, nullable=False, default="")
    # Schedule trigger: when enabled and interval_minutes is set, the
    # scheduler runs the workflow every N minutes (never overlapping).
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    interval_minutes = db.Column(db.Integer, nullable=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    runs = db.relationship("AutomationRun", backref="workflow",
                           lazy="select", cascade="all, delete-orphan",
                           order_by="AutomationRun.id.desc()")

    def __repr__(self):
        return f"<AutomationWorkflow {self.id} '{self.name}'>"


class AutomationBehavior(db.Model):
    """A reusable AI behavior (system prompt) the user manages and picks
    in the AI nodes' behavior field."""

    __tablename__ = "mod_automation_behavior"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    def __repr__(self):
        return f"<AutomationBehavior {self.id} '{self.name}'>"


class AutomationRun(db.Model):
    """One execution of a workflow, with a per-node log and final output."""

    __tablename__ = "mod_automation_run"

    STATUSES = ("queued", "running", "done", "error", "stopped", "interrupted")
    FINAL_STATUSES = ("done", "error", "stopped", "interrupted")
    TRIGGERS = ("manual", "schedule", "ai")

    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer,
                            db.ForeignKey("mod_automation_workflow.id"),
                            nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="queued",
                       index=True)
    trigger = db.Column(db.String(20), nullable=False, default="manual")
    # File the run was started from (drive → Run workflow), if any.
    trigger_file_id = db.Column(db.Integer, nullable=True)
    # JSON {param: value} collected in the run form for input.param nodes.
    params = db.Column(db.Text, nullable=True)
    # JSON list of per-node entries: {node, label, status, summary, ms}.
    log = db.Column(db.Text, nullable=False, default="")
    # JSON: {"results": [{node, kind, preview}], "files": [{file_id, name}]}.
    output = db.Column(db.Text, nullable=False, default="")
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_active(self):
        return self.status in ("queued", "running")

    def __repr__(self):
        return f"<AutomationRun {self.id} {self.status}>"
