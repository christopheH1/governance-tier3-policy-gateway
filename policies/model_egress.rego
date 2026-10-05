# Tier 3 decision for the model path of the policy gateway.
# Outcomes: deny or allow. Anything unmatched is denied.
#
# This policy is deterministic and does not read the prompt. It decides who may
# use which model, for a declared purpose, within limits. What the prompt says
# is a Tier 2 concern.
package ai_governance.tier3.model_egress

import rego.v1

default decision := {"outcome": "deny", "reasons": {"no_matching_rule"}}

decision := {"outcome": "deny", "reasons": deny_reasons} if {
	count(deny_reasons) > 0
} else := {"outcome": "allow", "reasons": {"all_constraints_satisfied"}} if {
	model_permitted
}

model := data.tier3.models[input.model.name]

role := data.tier3.roles[input.identity.role]

model_permitted if input.model.name in role.models

# --- deny -------------------------------------------------------------------
deny_reasons contains "identity_not_verified" if not input.identity.verified == true

deny_reasons contains "intent_not_declared" if not input.intent.purpose

deny_reasons contains "task_not_declared" if not input.intent.task_id

deny_reasons contains "intent_not_recognised" if {
	input.intent.purpose
	not data.tier3.purposes[input.intent.purpose]
}

deny_reasons contains "model_not_registered" if not model

deny_reasons contains "model_not_permitted" if not model_permitted

deny_reasons contains "max_tokens_exceeded" if input.model.max_tokens > model.max_tokens

# The count includes this request.
deny_reasons contains "rate_limit_exceeded" if input.usage.requests_this_minute > model.rate_per_minute

deny_reasons contains "token_budget_exceeded" if input.usage.tokens_today >= role.daily_token_budget

# --- facts the gateway needs alongside the decision ---------------------------
# The ceiling applied when the request names no max_tokens of its own.
max_tokens := model.max_tokens

location := model.location

# Tools the model may be offered: those the request offers that the role holds
# and the declared purpose covers. The gateway removes the rest before the call,
# so the model is never shown a tool the agent could not use.
# --- checking that a request fits its declared purpose --------------------------
# The declared purpose is a claim. Policy cannot read a request, so the gateway asks
# a checking model whether the request fits the purpose's description, and flags a
# mismatch for a person to look at. The check never blocks a call.
#
# The checking model belongs to the control plane. It sits beside the gateway, on a
# network agents cannot reach, and is not one of the models agents may use. So the
# judge is separate from the judged: an agent cannot call it, load it, or crowd it out.
intent_check := check if {
	data.tier3.intent_check.enabled
	purpose := data.tier3.purposes[input.intent.purpose]
	not purpose.check_model
	check := {"via": "control", "description": purpose.description, "record_text": record_flagged_text}
}

# A purpose may name a registered model as its checker instead. This is for tests,
# which use a stand-in. Such a model must be internal, so that no request text
# leaves the organisation for the sake of the check.
intent_check := check if {
	data.tier3.intent_check.enabled
	purpose := data.tier3.purposes[input.intent.purpose]
	data.tier3.models[purpose.check_model].location == "internal"
	check := {"via": "adapter", "model": purpose.check_model, "description": purpose.description, "record_text": record_flagged_text}
}

# Whether the words of a request are written to the ledger when the check flags it.
# For a request that is not flagged, the ledger holds a fingerprint and the task's declared
# statement (its first 200 characters), never the full text of the request or of a reply.
default record_flagged_text := false

record_flagged_text if {
	data.tier3.intent_check.record_flagged_text == true
}

permitted_tools contains tool if {
	some tool in input.model.offered_tools
	tool in role.tools
	tool in data.tier3.purposes[input.intent.purpose].tools
}
