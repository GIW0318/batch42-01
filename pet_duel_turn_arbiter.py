"""Turn-based pet duel arbiter.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``submit`` accepts exactly one command per player per turn.  A repeat gets
  ``turn_already_committed``.  Commands are ``{"type": "skill", "skill":
  name}`` or ``{"type": "switch", "pet": pet_id}``.
* ``resolve`` orders all switch commands before any skill, then orders
  skills by priority descending, effective speed at the start of the turn
  descending and finally player id.  Damage is computed from the
  start-of-turn HP and applied simultaneously, so a double knockout is a
  single ``draw`` with no rating change.  One random draw is consumed per
  damaging action that declares ``crit_chance``; the same seed therefore
  produces the same result for frame rates 30 and 60.
* Skill effects (``effect``) are applied after end-of-turn buff decay.
  ``{"speed": delta, "turns": n}`` adds to effective speed for the next n
  turns; ``{"stealth": True, "turns": 1}`` makes the pet un-hittable for the
  next turn and cover the turn it expires on, then disappears.
* Cooldowns and energy are validated for player commands and for AI
  commands alike.  AI picks the highest-power usable skill, ties broken by
  skill name; if none is usable it skips.  ``max_turns`` ends the match with
  ``draw_timeout`` instead of looping forever.
* Surrender resolves in the same turn as the last hit as a single result,
  applying the +/-25 rating change exactly once.
* ``state`` reports the next turn, current pets, hp, energy, cooldowns and
  buffs.  ``snapshot_at`` and ``turn_log`` expose historical turns including
  the action order, commands and seed.  ``export_state`` / ``load_state``
  restore hp, energy, cooldowns, buffs and turn counters without declaring a
  loss.
"""


class PetDuelTurnArbiter:
    def __init__(self, seed=1234, max_turns=50):
        raise NotImplementedError

    def create_match(self, match_id, player_a, player_b, pets_a, pets_b):
        raise NotImplementedError

    def submit(self, match_id, player, turn, command):
        raise NotImplementedError

    def surrender(self, match_id, player):
        raise NotImplementedError

    def take_over(self, match_id, player):
        raise NotImplementedError

    def resolve(self, match_id, turn, frame_rate=30):
        raise NotImplementedError

    def state(self, match_id):
        raise NotImplementedError

    def pet_state(self, match_id, player, pet_id):
        raise NotImplementedError

    def snapshot_at(self, match_id, turn):
        raise NotImplementedError

    def turn_log(self, match_id, turn):
        raise NotImplementedError

    def rating(self, player):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
