# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.347 | +2.9% | 0.248 | +1.8% | 0.500 | +0.7% | 2% | 413 (409) |
| arav_kyc_1 | 0.360 | +0.2% | 0.217 | +1.0% | 0.594 | -1.1% | 0% | 344 (352) |
| arav_renewal_0 | 0.443 | +9.3% | 0.267 | +1.3% | 0.586 | +7.0% | 40% | 416 (408) |
| kest_cancel_2 | 0.362 | +7.5% | 0.246 | +1.4% | 0.523 | +4.8% | 35% | 428 (418) |
| kest_kyc_1 | 0.355 | -0.1% | 0.214 | +0.0% | 0.595 | -0.3% | 0% | 370 (370) |
| kest_renewal_0 | 0.433 | +7.7% | 0.268 | +1.4% | 0.573 | +5.2% | 26% | 475 (403) |
| nort_cancel_2 | 0.367 | +8.1% | 0.248 | +1.8% | 0.523 | +4.9% | 38% | 417 (368) |
| nort_kyc_1 | 0.358 | +0.6% | 0.217 | +0.7% | 0.592 | -0.6% | 0% | 345 (318) |
| nort_renewal_0 | 0.441 | +8.9% | 0.268 | +1.3% | 0.585 | +6.8% | 38% | 457 (419) |
| tide_cancel_2 | 0.364 | +6.0% | 0.248 | +1.4% | 0.521 | +4.0% | 33% | 398 (401) |
| tide_kyc_1 | 0.362 | +1.7% | 0.217 | +1.2% | 0.599 | +0.2% | 0% | 340 (376) |
| tide_renewal_0 | 0.443 | +9.4% | 0.268 | +1.6% | 0.585 | +7.1% | 41% | 468 (425) |
| ALL | 0.386 | +5.3% | 0.244 | +1.3% | 0.564 | +3.3% | 22% | 4871 (4667) |

## Changes applied by the controller

49 applied — neutral: 32, beneficial: 17

- tide_cancel_2 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- arav_cancel_2 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- tide_renewal_0 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_renewal_0 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_renewal_0 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_renewal_0 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v3 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- kest_cancel_2 v3 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nort_cancel_2 v3 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v3 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.120 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.120 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.120 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.025 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v5 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- tide_renewal_0 v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- kest_cancel_2 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- tide_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v8 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v9 (controller_auto) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- kest_cancel_2 v10 (controller_auto) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- kest_cancel_2 v11 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- arav_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- kest_cancel_2 v12 (controller_approved) dims=['voice'] engageΔ=+0.067 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_renewal_0 v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.120 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v5 (controller_approved) dims=['voice'] engageΔ=+0.072 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v7 (controller_approved) dims=['voice'] engageΔ=+0.055 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v13 (controller_approved) dims=['welcome_message'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v8 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v5 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- tide_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 41,
  "decisions": {
    "approved": 35,
    "rejected": 5,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 5
  },
  "applied_via_approval": 35,
  "auto_applied": 14,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "arav_cancel_2": 2,
    "arav_kyc_1": 1,
    "arav_renewal_0": 2,
    "nort_cancel_2": 2,
    "nort_kyc_1": 1,
    "nort_renewal_0": 2,
    "tide_cancel_2": 2,
    "tide_kyc_1": 1,
    "tide_renewal_0": 2,
    "kest_cancel_2": 2,
    "kest_renewal_0": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.9,
    "4": 0.903
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.902,
    "4": 0.901
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.899,
    "4": 0.901
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.9,
    "4": 0.901
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0