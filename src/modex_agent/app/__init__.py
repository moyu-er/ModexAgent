"""Application-level skeleton (W3a/W4b) — the top of the layering tree.

- :mod:`modex_agent.app.config` — ``AppConfig``, the root YAML config face.
- :mod:`modex_agent.app.config_domain` — config-domain helpers (field
  descriptors, secret masking, merge, atomic YAML write).
- :mod:`modex_agent.app.models` — the model universe: the multi-provider
  model registry, the per-turn selection/pinned providers, the choice
  carrier, and the ``PoolModelAssembly`` implementation.
- :mod:`modex_agent.app.roots` — ``AppAssemblyRoots``, the three explicit
  roots (config / resource / workspace-home) of one application assembly.
- :mod:`modex_agent.app.runnable` — ``RunnableAppService``, the concrete
  framework-runnable default (declaration boot → ``create_pool`` per
  declared pool, framework defaults, single-turn driver).
- :mod:`modex_agent.app.service` — ``AppService``, the lifecycle skeleton
  (roots, component registry, shared persistence, routing store).
- :mod:`modex_agent.app.supervisor` — the process-level supervisor
  (crash-restart / stable-period / signals) driving an ``AppService``.
"""
