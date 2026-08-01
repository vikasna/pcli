I am thinking about a project - a cli like opencode in Python. It must have a TUI, session management with import and export, cost tracking, permission handling, guardrails, sandboxed execution. Should be able to connect using gateway url and api-key.
With these new features:
 - discovery tool for LLM to search and use any python function or packages installed by default
	- helps LLM to use any python functions or packages by lazy loading them
 - toolbox for LLM that can discover OS and softwares (like kubernetes, sun grid engine, kafka, apache httpd, etc) installed on the machine, its version and add tools into the toolbox for it to use in future
	- discovery of softwares will be triggered by user
	- tools added to the toolbox for a OS or software must include all admin, operations and user facing commands of the version discovered.. so that if user wants to do something LLM uses this added tool
	