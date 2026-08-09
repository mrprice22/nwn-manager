"""nwn_wiki.sim -- the build-time combat simulation behind the counter-gear
report.

* :mod:`nwn_wiki.sim.combat` -- expected-value attack/defence profiles, damage
  per round, rounds-to-kill and the fight scoring the kit solver maximises.
* :mod:`nwn_wiki.sim.pc` -- the reference PC's feat budget and kit flattening.

This package deliberately re-exports nothing; import from the submodules.
"""

from __future__ import annotations
