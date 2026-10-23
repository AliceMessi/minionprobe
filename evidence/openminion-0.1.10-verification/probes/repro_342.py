# The #342 issue reproducer, unchanged except for printing which openminion is imported
# and whether the runtime-root index exists.
import logging, sys
from pathlib import Path
logging.disable(logging.CRITICAL)
import openminion
print("openminion", openminion.__version__, openminion.__file__)
from openminion import Agent, APIRuntime
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

root = Path(sys.argv[1]).resolve()
config = OpenMinionConfig()
config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
config.default_agent = "openminion"
config.storage.path = str(root / ".openminion" / "state.db")
save_config(config, str(root / "agents.json"))

runtime = APIRuntime.from_config_path(str(root / "agents.json"), home_root=root, data_root=root / ".openminion")
with Agent(runtime=runtime, session_id="artifact-root") as agent:
    print(agent.run("hello").run_state)
runtime.close()
cwd = Path.cwd()
print("written under cwd:", sorted(p.relative_to(cwd).as_posix() for p in cwd.rglob("*") if p.is_file()))
home = Path(sys.argv[2]).resolve()
print("written under home:", sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()))
print("runtime-root index exists:", (root / ".openminion" / "artifact" / "index.db").is_file())
