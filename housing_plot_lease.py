"""Housing plot lease and furniture custody.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* Furniture has a unique instance id, an owner, a size ``(w, h)`` and a
  location: ``("bag", owner)``, ``("house", plot)``, ``("storage", plot)``
  or ``("mail", owner)``.  ``place`` consumes one bag item, checks the whole
  rotated footprint against other furniture in the plot and raises
  ``SlotOccupiedError`` without consuming anything on conflict.  Concurrent
  placements in the same slot leave exactly one winner.
* ``rotate`` recomputes the footprint; if the rotated footprint collides it
  raises ``SlotOccupiedError`` and keeps the previous rotation and slots.
  ``move`` accepts ``expected_slot`` and raises ``StaleMoveError`` when the
  furniture already moved, so two roommates cannot duplicate it.
* Plot permissions are owner-controlled.  ``revoke_access`` takes effect
  immediately: an old teleport token no longer allows entry
  (``PermissionDeniedError``).  Visitors may edit only when
  ``public_edit`` is True.
* ``renew`` charges rent once and extends ``lease_end`` by ``days``.  An
  insufficient wallet raises ``PaymentError`` and changes nothing.  Lease
  expiry depends only on the supplied server time, never the timezone tag.
* ``start_move_out`` is atomic: a mail failure at ``mail_fail_at`` rolls the
  whole move back.  Successful packaging fills mails up to
  ``mail_attachment_limit`` and sends the remainder to storage, never
  dropping an item.  ``crash_after`` leaves an in-progress move that
  ``resume_move_out`` finishes after a restart with every furniture id in
  exactly one location.
* ``reclaim_expired`` only reclaims after ``lease_end`` and writes one
  audit entry containing the lease version and operator.
* ``import_template`` rejects foreign furniture ids as a whole before
  placing anything.
"""


class PermissionDeniedError(Exception):
    """Raised when an actor may not edit or enter the plot."""


class SlotOccupiedError(Exception):
    """Raised when a footprint overlaps existing furniture."""


class StaleMoveError(Exception):
    """Raised when a concurrent move already changed the expected slot."""


class InvalidTemplateError(Exception):
    """Raised when a template references furniture owned by somebody else."""


class PaymentError(Exception):
    """Raised when the wallet cannot pay the rent."""


class HousingPlotLease:
    def __init__(self, mail_attachment_limit=3, slot_count=32):
        raise NotImplementedError

    def add_furniture(self, furniture_id, owner, size=(1, 1)):
        raise NotImplementedError

    def set_wallet(self, player, gold):
        raise NotImplementedError

    def wallet(self, player):
        raise NotImplementedError

    def create_plot(
        self,
        plot_id,
        owner,
        lease_end,
        version=1,
        timezone="UTC",
        visitor_access=False,
    ):
        raise NotImplementedError

    def set_permission(self, plot_id, actor, public_edit=None, visitor_access=None):
        raise NotImplementedError

    def grant_access(self, plot_id, actor, visitor):
        raise NotImplementedError

    def revoke_access(self, plot_id, actor, visitor):
        raise NotImplementedError

    def enter(self, plot_id, visitor, teleport=None, now=0.0):
        raise NotImplementedError

    def place(self, plot_id, actor, furniture_id, slot, rotation=0):
        raise NotImplementedError

    def rotate(self, plot_id, actor, furniture_id, rotation):
        raise NotImplementedError

    def move(self, plot_id, actor, furniture_id, slot, expected_slot=None):
        raise NotImplementedError

    def store(self, plot_id, actor, furniture_id):
        raise NotImplementedError

    def start_move_out(self, plot_id, actor, mail_fail_at=None, crash_after=None):
        raise NotImplementedError

    def resume_move_out(self, plot_id):
        raise NotImplementedError

    def renew(self, plot_id, actor, now, days=30, rent=100):
        raise NotImplementedError

    def reclaim_expired(self, plot_id, now, operator):
        raise NotImplementedError

    def import_template(self, plot_id, actor, template):
        raise NotImplementedError

    def house_furniture(self, plot_id):
        raise NotImplementedError

    def furniture_slots(self, furniture_id):
        raise NotImplementedError

    def furniture_rotation(self, furniture_id):
        raise NotImplementedError

    def furniture_location(self, furniture_id):
        raise NotImplementedError

    def storage(self, plot_id):
        raise NotImplementedError

    def mailbox(self, owner):
        raise NotImplementedError

    def audit_log(self, plot_id):
        raise NotImplementedError

    def lease_status(self, plot_id, now):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
