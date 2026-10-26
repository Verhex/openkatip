# Pipeline research

Research only the selected ICP segment using granted Claude server web search.
Return the supplied JSON schema. Include source URLs and score evidence for each
exact ICP criterion. Use only public organization facts; no personal contacts.
Briefs must be Turkish and at most 120 words. Use supported icp-ai/icp-erp/icp-hr
tags only when justified. Return fewer candidates if evidence is missing.
Never inspect customer CRM in the model step; deterministic code performs dedupe
and proposes a lead for owner review. Never write Odoo records yourself.
