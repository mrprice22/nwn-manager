"""nwn_wiki.sim -- the build-time combat simulation behind the counter-gear
report.

* :mod:`nwn_wiki.sim.combat` -- expected-value attack/defence profiles, damage
  per round, rounds-to-kill and the fight scoring the kit solver maximises.
* :mod:`nwn_wiki.sim.pc` -- the reference PC: its feat budget, kit flattening
  and the profiles a level-N Fighter fields.
* :mod:`nwn_wiki.sim.gear` -- the player-attainable gear pool and the greedy
  kit solver that picks (then minimises) a winning loadout.

This package deliberately re-exports nothing; import from the submodules.
"""

from __future__ import annotations
