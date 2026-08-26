# Product Doctrine

Read this before writing any code in this repository.

1. Never add a feature because it sounds impressive.
2. Every feature must solve a problem observed during a pilot.
3. AI explains decisions; it does not invent accounting facts.
   (The LLM handles language — extraction, classification, explanation,
   translation. Accounting truth is always deterministic code.)
4. Every automation must be reviewable.
5. Every AI recommendation must be measurable.
6. Time saved is more important than AI complexity.
7. Trust is more important than intelligence.
8. Remove features accountants don't use.
9. If a feature cannot improve a KPI or solve a pilot observation, don't build it.

The one-sentence introduction — never "we built an AI accounting platform":
« Nous aidons les cabinets comptables marocains à valider leurs factures plus
vite, à intercepter les erreurs avant écriture et à accélérer la clôture
mensuelle — tout en gardant le logiciel comptable qu'ils utilisent déjà. »

The demo starts with the accountant's day, not the software:
invoices arrive → AI extracts → AI explains → AI flags anomalies →
the accountant approves → export to the software the firm already uses.

The next commit must cite the pilot observation that motivated it.
