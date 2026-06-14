"""UiPath Coded Agent for FraudCase AI.

The same audit logic as the FastAPI demo, repackaged as a UiPath coded agent
(built with the `uipath` Python SDK) so it deploys to Automation Cloud and is
orchestrated by a UiPath Maestro Case process.

Because coded agents run request/response on Serverless Robots, the long-running
human-in-the-loop flow is split into three entrypoints that Maestro calls between
its human tasks:

    plan        -> Gate 1 (auditor reviews the plan)
    investigate -> Gate 2 (auditor reviews the proposed findings)
    finalize    -> writes the approved findings + audit log to Data Service
"""

from fraudcase_ai.coded_agent.main import finalize, investigate, plan

__all__ = ["plan", "investigate", "finalize"]
