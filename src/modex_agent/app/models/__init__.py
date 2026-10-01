"""The framework model universe (W4b).

Three cooperating modules over one ``model.yml`` deployment config:

- :mod:`modex_agent.app.models.registry` — the parsed multi-provider model
  registry (``ModelCfg`` / ``ProviderCfg`` / ``ResolvedModel`` /
  :class:`ModelRegistry`) with ``synthesize_llm_config`` and the
  placeholder fallback for unconfigured deployments.
- :mod:`modex_agent.app.models.provider` — the per-turn selection proxy
  (:class:`ModelSelectionProvider`, ContextVar-driven), the explicit
  :class:`PinnedModelProvider`, and the D-5 per-agent pin resolver
  (:func:`resolve_agent_llm_pins`).
- :mod:`modex_agent.app.models.choice` — the cross-broker per-turn choice
  carrier (:class:`ModelChoiceRegistry`, ``current_model_choice``) and the
  :class:`ModelChoiceBindHook` turn-binding hook.
- :mod:`modex_agent.app.models.assembly` — :class:`ModelRegistryAssembly`,
  the framework :class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly`
  implementation that threads the registry into pool assembly.
"""
