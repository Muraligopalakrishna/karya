"""Importing this package registers every tool in karya.registry.TOOLS."""
from . import accounts, browser, email_tools, finance, jobs, memory_tools, pc, resume, skills, web, website  # noqa: F401
from . import job_sources, funding  # noqa: F401,E402 - after jobs (they use its helpers)
from . import autofill  # noqa: F401,E402 - uses browser and jobs
from . import market_talk  # noqa: F401,E402 - uses web and finance
from . import work_history  # noqa: F401,E402 - uses resume and job_sources
from .. import phone, scheduler  # noqa: F401,E402 - background agents and tasks from WhatsApp
