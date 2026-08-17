# Local-EZAI

## Identity

This repository is Local-EZAI.

Local-EZAI already exists.

This is NOT a greenfield project.

This is NOT a new product.

This is NOT a Claude Code clone.

This is NOT a Cursor clone.

This repository already contains:

- Chat capabilities
- Agent capabilities
- Model integrations
- Configuration systems
- APIs
- User interface
- Existing workflows

---

# Critical Rule

Never replace Local-EZAI.

Never rebuild Local-EZAI from scratch.

Never remove existing functionality unless explicitly instructed.

Always extend.

Always integrate.

Always preserve backward compatibility.

The objective is:

Local-EZAI
+
Autonomous Software Engineering

not

New Product
replacing
Local-EZAI

---

# Product Vision

Local-EZAI is an AI Platform.

Autonomous Software Engineering is one feature of that platform.

Target Product:

Local-EZAI

├── Chat
├── Agents
├── Models
├── MCP
├── Knowledge
├── Tools
└── Autonomous SWE

The Autonomous SWE subsystem must integrate into Local-EZAI.

---

# Existing Functionality First

Before implementing anything:

Inspect:

- API
- UI
- Agent runtime
- Model system
- Configuration
- Existing workflows

Determine:

Can this capability be integrated?

If yes:

Integrate.

Do not replace.

---

# Architecture Principles

Follow:

- Clean Architecture
- SOLID
- Dependency Injection
- Modular Design

Avoid:

- Monoliths
- Tight Coupling
- Breaking Changes

---

# Autonomous SWE Vision

Add the following capabilities incrementally:

Planner Agent

Coding Agent

Validation Agent

Debug Agent

Git Agent

Browser QA Agent

Memory Agent

Documentation Agent

Evolution Agent

Sprint Agent

Each capability must be added as a feature.

Never as a replacement.

---

# Planner Agent

Responsibilities:

- Requirement analysis
- Task decomposition
- Dependency analysis

Planner creates plans.

Planner does not generate implementation code.

---

# Coding Agent

Responsibilities:

- Read repository
- Modify files
- Create files
- Generate tests

Never rewrite repositories blindly.

Use targeted modifications.

---

# Validation Agent

Responsibilities:

- Tests
- Build
- Lint
- Type validation

Validation must happen after code generation.

---

# Debug Agent

Responsibilities:

- Root cause analysis
- Fix generation
- Re-validation

Never patch symptoms.

Fix causes.

---

# Browser QA Agent

Use Playwright.

Validate:

- UI
- Workflows
- Browser console errors

Browser QA is part of validation.

---

# Memory Agent

Store:

- decisions
- architecture
- lessons learned
- recurring fixes

Location:

.agent/

---

# Documentation Agent

Maintain:

docs/

Generate:

- USER_GUIDE.md
- OPERATION_MANUAL.md
- MAINTENANCE_GUIDE.md
- RELEASE_NOTES.md

Documentation is mandatory.

---

# Evolution Agent

The purpose of Evolution Agent is:

Improve Local-EZAI.

Not replace Local-EZAI.

Workflow:

Analyze
↓
Propose
↓
Implement
↓
Validate
↓
Pull Request
↓
Human Approval

---

# Human Governance

Autonomous does not mean uncontrolled.

The following require approval:

- Architecture migrations
- Core runtime changes
- Model replacement
- Production releases
- Pull request merges

Agents propose.

Humans approve.

---

# Model Routing

agent_model_map:

  planner:
    primary: hermes3
    fallback: deepseek-r1

  coder:
    primary: qwen3-coder
    fallback: deepseek-r1

  debugger:
    primary: deepseek-r1
    fallback: hermes3

  reviewer:
    primary: llama3

  documentation:
    primary: llama3

  memory:
    primary: hermes3

  evolution:
    primary: deepseek-r1

---

# Self Evolution

Local-EZAI should eventually be capable of:

- maintaining itself
- documenting itself
- testing itself
- validating itself
- improving itself

All self-improvements must:

- be documented
- be tested
- be benchmarked
- create pull requests

Human approval is required.

---

# Session Startup Rules

At the beginning of every session:

1. Read CLAUDE.md
2. Read .agent/roadmap.md
3. Read .agent/decisions.md
4. Read architecture documents
5. Analyze repository state

Before coding:

Create a plan.

Before finishing:

Validate.

---

# Bootstrap Exit Strategy

Claude Code is only a bootstrap engineer.

The final objective is:

Human
→ Roadmap
→ Local-EZAI

not

Human
→ Claude Code
→ Local-EZAI

Local-EZAI should eventually maintain itself.
