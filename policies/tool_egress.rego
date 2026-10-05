# Tier 3 decision for the tool path of the policy gateway.
# Outcomes: deny, refer (mode hold or flag), allow. Anything unmatched is denied.
#
# The gateway fills input.identity after Tessera has validated the credential,
# so this policy never evaluates an identity the agent wrote itself.
package ai_governance.tier3.tool_egress

import rego.v1

default decision := {"outcome": "deny", "reasons": {"no_matching_rule"}}

decision := {"outcome": "deny", "reasons": deny_reasons} if {
	count(deny_reasons) > 0
} else := {"outcome": "refer", "mode": referral_mode, "reasons": refer_reasons} if {
	count(refer_reasons) > 0
} else := {"outcome": "allow", "reasons": {"all_constraints_satisfied"}} if {
	tool_permitted
	intent_permitted
}

tool_registered if data.tier3.tools[input.tool.name]

tool_permitted if input.tool.name in data.tier3.roles[input.identity.role].tools

intent_permitted if input.tool.name in data.tier3.purposes[input.intent.purpose].tools

# --- deny -------------------------------------------------------------------
deny_reasons contains "identity_not_verified" if not input.identity.verified == true

deny_reasons contains "tool_not_registered" if not tool_registered

deny_reasons contains "tool_not_whitelisted" if not tool_permitted

deny_reasons contains "intent_not_declared" if not input.intent.purpose

deny_reasons contains "task_not_declared" if not input.intent.task_id

deny_reasons contains "intent_not_recognised" if {
	input.intent.purpose
	not data.tier3.purposes[input.intent.purpose]
}

deny_reasons contains "intent_tool_mismatch" if {
	data.tier3.purposes[input.intent.purpose]
	not intent_permitted
}

# --- refer ------------------------------------------------------------------
refer_reasons contains "sensitive_tool" if input.tool.name in data.tier3.referral.tools

refer_reasons contains "row_threshold" if input.tool.params.limit > data.tier3.referral.max_rows

refer_reasons contains "tool_unclassified" if {
	tool_registered
	not data.tier3.tools[input.tool.name].class
}

# Hold unless the tool is explicitly a reversible read.
default referral_mode := "hold"

referral_mode := "flag" if data.tier3.tools[input.tool.name].class == "reversible_read"

# --- facts the gateway needs alongside the decision ---------------------------
# Where the tool lives. The agent names a tool and never supplies a URL.
destination := data.tier3.tools[input.tool.name].destination

# How long a held call waits for a human before it is denied.
hold_timeout_seconds := seconds if {
	seconds := data.tier3.tools[input.tool.name].hold_timeout_seconds
} else := seconds if {
	seconds := data.tier3.referral.hold_timeout_seconds
} else := 900
