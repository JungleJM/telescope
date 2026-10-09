"""Infant RSV analysis.

From the folder that holds `rsv`:

    python rsv build              # the analysis parquets, and the build page
    python rsv report [section]
    python rsv compare <grouping> [section]
    python rsv admission [section]
    python rsv all                # a first run of every page

In Python, the same functions:

    import rsv
    settings = rsv.load_settings()
    visits = rsv.load(settings)
    rsv.summarize(visits[visits.age_days < 91])
    print(rsv.compare(settings, "era").render())
"""

from .analysis import admission, compare, load, report, section, summarize
from .build import build
from .config import RsvError, load_settings

__all__ = ["admission", "build", "compare", "load", "load_settings", "report", "section", "summarize", "RsvError"]
