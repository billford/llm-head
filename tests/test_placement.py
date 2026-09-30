"""Placement rules (spec §4.3), using real model sizes measured on 16 GB RTX 5060 Ti hosts."""

from llm_head.placement import Decision, HostSnapshot, Kind, LoadedModel, ModelFacts, choose

GiB = 1024**3
USABLE = (16311 - 512) * 1024 * 1024  # vram_mb minus the default reserve

# /api/ps sizes observed on 2026-09-30 (weights + KV cache at 4k context).
QWEN = "qwen2.5vl:7b-q4_k_m"
LLAMA = "llama3.2:3b"
GPTOSS = "gpt-oss:20b"
SIZES = {QWEN: 5_864_591_195, LLAMA: 3_097_881_476, GPTOSS: int(11.89 * GiB)}
ALL = frozenset(SIZES)


def facts(model: str, **kw) -> ModelFacts:
    base = dict(vram_bytes=SIZES[model], typical_duration=1.0, load_time=4.0)
    base.update(kw)
    return ModelFacts(**base)


def host(name: str, loaded=(), inflight=None, routable=True, **kw) -> HostSnapshot:
    return HostSnapshot(
        name=name,
        routable=routable,
        usable_vram_bytes=kw.pop("usable", USABLE),
        slots_per_model=kw.pop("slots", 2),
        max_loaded_models=kw.pop("max_loaded", 2),
        installed=kw.pop("installed", ALL),
        loaded={m: LoadedModel(SIZES.get(m, GiB), last_used=i) for i, m in enumerate(loaded)},
        inflight=dict(inflight or {}),
    )


def test_unknown_model_is_rejected_404():
    d = choose("nope:1b", [host("a")], ModelFacts(vram_bytes=GiB))
    assert d == Decision(Kind.REJECT, "model_not_found", status=404)


def test_all_hosts_down_is_503():
    d = choose(LLAMA, [host("a", routable=False)], facts(LLAMA))
    assert (d.kind, d.status) == (Kind.REJECT, 503)


def test_ties_alternate_instead_of_always_picking_first_host():
    """The Olla 2:1 skew: least-connections at 0/0 always chose xmas."""
    hosts = [host("xmas", [QWEN, LLAMA]), host("european", [QWEN, LLAMA])]
    picks = [choose(QWEN, hosts, facts(QWEN), rotation=i).host for i in range(4)]
    assert sorted(picks) == ["european", "european", "xmas", "xmas"]


def test_prefers_loaded_host_over_cold_load():
    """xmas swapped models on 59% of requests because routing ignored what was loaded."""
    hosts = [host("xmas", [GPTOSS]), host("european", [QWEN, LLAMA])]
    for r in range(2):
        d = choose(QWEN, hosts, facts(QWEN), rotation=r)
        assert (d.kind, d.host, d.cold) == (Kind.DISPATCH, "european", False)


def test_prefers_less_busy_loaded_host():
    hosts = [host("xmas", [QWEN], inflight={QWEN: 1}), host("european", [QWEN])]
    assert choose(QWEN, hosts, facts(QWEN)).host == "european"


def test_waits_for_slot_when_waiting_is_cheaper_than_cold_load():
    hosts = [
        host("xmas", [QWEN, LLAMA], inflight={QWEN: 2}),
        host("european", [GPTOSS], inflight={GPTOSS: 1}),
    ]
    d = choose(QWEN, hosts, facts(QWEN, typical_duration=0.6, load_time=4.0))
    assert (d.kind, d.reason) == (Kind.WAIT, "slots_busy")


def test_loads_replica_when_queue_is_long():
    hosts = [host("xmas", [QWEN], inflight={QWEN: 2}), host("european", [])]
    d = choose(QWEN, hosts, facts(QWEN, typical_duration=3.0, load_time=4.0), queued_ahead=4)
    assert (d.kind, d.host, d.cold, d.reason) == (Kind.DISPATCH, "european", True, "replica_cheaper_than_wait")


def test_cold_load_prefers_host_with_free_memory_over_eviction():
    hosts = [host("xmas", [QWEN, LLAMA]), host("european", [])]
    d = choose(GPTOSS, hosts, facts(GPTOSS))
    assert (d.host, d.evict) == ("european", ())


def test_never_evicts_a_model_with_requests_in_flight():
    """Root cause of the 12 tok/s gpt-oss runs: it landed where qwen was busy."""
    hosts = [
        host("xmas", [QWEN, LLAMA], inflight={QWEN: 1, LLAMA: 1}),
        host("european", [QWEN, LLAMA], inflight={QWEN: 1}),
    ]
    d = choose(GPTOSS, hosts, facts(GPTOSS))
    # european can free enough by evicting only its idle llama? No: 11.9 + 5.5 GiB > 15.4.
    # So gpt-oss must wait rather than spill onto the CPU.
    assert (d.kind, d.reason) == (Kind.WAIT, "no_capacity")


def test_evicts_only_idle_models_and_as_few_as_needed():
    hosts = [
        host("xmas", [QWEN, LLAMA], inflight={QWEN: 1}),
        host("european", [LLAMA, QWEN]),  # llama least recently used
    ]
    d = choose(GPTOSS, hosts, facts(GPTOSS))
    assert d.host == "european"
    # gpt-oss (11.9 GiB) + llama (2.9 GiB) fits in 15.4 GiB, so only qwen needs to go.
    assert d.evict == (QWEN,)


def test_max_loaded_models_forces_eviction_even_with_free_memory():
    hosts = [host("a", [LLAMA, "nomic-embed-text:latest"], installed=ALL | {"nomic-embed-text:latest"})]
    d = choose(QWEN, hosts, facts(QWEN))
    assert d.kind == Kind.DISPATCH and len(d.evict) == 1


def test_keep_warm_is_spared_when_possible():
    hosts = [host("xmas", [QWEN, LLAMA]), host("european", [QWEN, LLAMA])]
    warm = {QWEN: facts(QWEN, keep_warm=2), LLAMA: facts(LLAMA)}
    d = choose(GPTOSS, hosts, facts(GPTOSS), warm_facts=warm)
    assert d.kind == Kind.DISPATCH
    # Evicting qwen is the only way to fit gpt-oss, so keep_warm yields rather than
    # leaving gpt-oss waiting forever.
    assert QWEN in d.evict


def test_keep_warm_prefers_evicting_other_models():
    big = frozenset(ALL | {"llava:latest"})
    SIZES["llava:latest"] = int(4.4 * GiB)
    try:
        hosts = [host("xmas", [QWEN, LLAMA], installed=big), host("european", [QWEN, "llava:latest"], installed=big)]
        warm = {QWEN: facts(QWEN, keep_warm=2), LLAMA: facts(LLAMA), "llava:latest": facts("llava:latest")}
        d = choose(GPTOSS, hosts, facts(GPTOSS), warm_facts=warm)
        assert QWEN in d.evict  # no plan avoids it, so the cheapest plan is used
        warm[QWEN] = facts(QWEN, keep_warm=1)
        # qwen stays loaded on one host either way, so evicting it is allowed at no penalty.
        d = choose(GPTOSS, hosts, facts(GPTOSS), warm_facts=warm)
        assert d.kind == Kind.DISPATCH
    finally:
        del SIZES["llava:latest"]


def test_home_host_breaks_ties():
    hosts = [host("xmas", []), host("european", [])]
    for r in range(3):
        d = choose(GPTOSS, hosts, facts(GPTOSS, home=("european",)), rotation=r)
        assert d.host == "european"


def test_model_only_installed_on_some_hosts_routes_there():
    hosts = [host("xmas", [], installed=frozenset({LLAMA})), host("european", [])]
    assert choose(GPTOSS, hosts, facts(GPTOSS)).host == "european"


def test_excluded_host_is_skipped_on_retry():
    hosts = [host("xmas", [QWEN]), host("european", [])]
    d = choose(QWEN, hosts, facts(QWEN), exclude=frozenset({"xmas"}))
    assert (d.host, d.cold) == ("european", True)


def test_unavoidable_cpu_offload_is_served_when_host_idle():
    huge = ModelFacts(vram_bytes=40 * GiB)
    hosts = [host("a", [LLAMA], installed=frozenset({"big:70b"}))]
    d = choose("big:70b", hosts, huge)
    assert (d.kind, d.reason, d.evict) == (Kind.DISPATCH, "cpu_offload_unavoidable", (LLAMA,))
    hosts[0].inflight[LLAMA] = 1
    assert choose("big:70b", hosts, huge).kind == Kind.WAIT


def test_allow_cpu_offload_skips_memory_check():
    huge = ModelFacts(vram_bytes=40 * GiB, allow_cpu_offload=True)
    hosts = [host("a", [], installed=frozenset({"big:70b"}))]
    assert choose("big:70b", hosts, huge).kind == Kind.DISPATCH


def test_does_not_pile_onto_a_host_that_is_still_loading():
    """Phase 2 shadow run: llama3.2 took 9.6s to load on xmas and five requests queued
    behind that load while european was idle and could load it in about a second."""
    xmas = host("xmas", [LLAMA], inflight={LLAMA: 2})
    xmas.loaded[LLAMA].ready_in = 8.0
    hosts = [xmas, host("european", [])]
    d = choose(LLAMA, hosts, facts(LLAMA, typical_duration=0.5, load_time=1.5))
    assert (d.kind, d.host, d.cold) == (Kind.DISPATCH, "european", True)


def test_a_nearly_finished_load_is_still_worth_waiting_for():
    xmas = host("xmas", [LLAMA])
    xmas.loaded[LLAMA].ready_in = 0.5
    hosts = [xmas, host("european", [])]
    d = choose(LLAMA, hosts, facts(LLAMA, typical_duration=0.5, load_time=3.0))
    assert (d.kind, d.host, d.reason) == (Kind.DISPATCH, "xmas", "loading")


def test_model_loaded_at_other_context_size_is_not_warm():
    """qwen loaded at 4096 can't serve an 8192 request without a reload."""
    a = host("xmas", [QWEN])
    a.loaded[QWEN].context_length = 4096
    b = host("european", [QWEN])
    b.loaded[QWEN].context_length = 8192
    d = choose(QWEN, [a, b], facts(QWEN), ctx=8192)
    assert (d.host, d.cold) == ("european", False)
    d = choose(QWEN, [a, b], facts(QWEN), ctx=None)  # default 4096
    assert (d.host, d.cold) == ("xmas", False)


def test_reload_for_context_only_where_the_model_is_idle():
    """Ollama reloads only once the loaded copy is idle; a busy host would starve the request."""
    a = host("xmas", [QWEN], inflight={QWEN: 1})
    b = host("european", [QWEN])
    for h in (a, b):
        h.loaded[QWEN].context_length = 4096
    d = choose(QWEN, [a, b], facts(QWEN), ctx=8192)
    assert (d.kind, d.host, d.reason, d.evict[:1]) == (Kind.DISPATCH, "european", "reload_context", (QWEN,))


def test_context_reload_waits_when_every_copy_is_busy():
    hosts = [host(n, [QWEN], inflight={QWEN: 1}) for n in ("xmas", "european")]
    for h in hosts:
        h.loaded[QWEN].context_length = 4096
    d = choose(QWEN, hosts, facts(QWEN), ctx=8192)
    assert d.kind == Kind.WAIT
