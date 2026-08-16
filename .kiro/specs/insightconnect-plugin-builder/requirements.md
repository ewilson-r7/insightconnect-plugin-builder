# Requirements Document

> **Revision note.** This document was substantially revised after the initial
> implementation. The first version specified a plugin *spec editor*: it required
> the tool to produce a schema-valid `plugin.spec.yaml` and to report validation
> results, but never required that the plugin it produced actually run. Every
> requirement in it was implemented and the test suite was green, and the tool
> still emitted plugins with unparseable Python, no API client, stub connection
> tests, and unimplemented unit tests. The specification was satisfied and the
> output was unusable.
>
> The revision changes three things: implementation is **delegated to an agent
> with tools** rather than assembled from LLM text completions (Requirement 3,
> Requirement 26); validation is **corrective** rather than advisory
> (Requirement 8); and there is now an explicit, testable **definition of done**
> for a generated plugin (Requirement 27). Requirements covering versioning,
> credentials, audit, export, project folders, and entry modes were accurate and
> are unchanged.

## Introduction
The InsightConnect Plugin Builder is a locally-run, self-hosted single-user tool that lets a user create new Rapid7 InsightConnect plugins, or extend existing ones, by describing what they want in natural language. The user downloads the tool and runs it on their own machine or self-managed infrastructure; there is no hosted backend service and no multi-user account model.

The tool's purpose is to produce a plugin that **works on first import** -- not a plugin-shaped directory tree. It achieves this by combining deterministic scaffolding, produced by the insight-plugin CLI, with **delegated implementation**: the Kiro CLI is run as an agent inside the plugin's own working directory, where it reads the spec, writes the interdependent source files, runs the toolchain, reads the failures, and fixes them. The rules it follows are the operator's own InsightConnect plugin skills and steering, referenced as agent resources rather than restated inside this tool, so there is one rulebook and it cannot drift.

The user interacts through a conversational interface, sees a live graphical representation of the plugin structure (connection, actions, triggers, tasks, and their input/output schemas), and receives auto-generated plugin documentation. Once satisfied, the user builds the plugin into a `.plg` artifact and/or pushes it directly into an InsightConnect tenant through the InsightConnect API using credentials they provide. The tool offers three entry modes: create a net-new plugin, iterate on a previously created custom plugin, or enhance an existing production plugin sourced read-only from the public (rapid7/insightconnect-plugins) or private (komand-plugins) repositories.

The tool stores each plugin's work in a per-plugin project folder on the local filesystem for historical lookup and reuse, records each export, and automatically increments the plugin version when a prior export exists so that tenant imports do not fail on version collisions. Every generated plugin has the string `_custom` appended to its vendor field so custom plugins are visually distinct from production Rapid7 plugins. Because the tool runs locally for a single operator, it includes an optional local access guard, credential protection, validation guardrails, cost controls, and audit logging.
## Glossary
- **Plugin_Builder**: The overall application described by this document, responsible for orchestrating conversation, generation, validation, build, and export.
- **Conversation_Interface**: The user-facing chat/UI component through which a user describes the desired plugin and iterates on it.
- **Generation_Engine**: The component that produces plugin artifacts, combining deterministic scaffolding with delegated implementation.
- **Deterministic_Scaffolder**: The subcomponent of the Generation_Engine that produces plugin structure (directories, boilerplate, spec skeleton) via the Insight_Plugin_CLI without LLM calls.
- **Plugin_Agent**: The Kiro_CLI run as an *agent* -- with file-read, file-write, and shell tools, and with the plugin's working directory as its working directory -- to implement the plugin's hand-written source. It writes files itself and runs the toolchain to check its own work. It is the component that does the development work.
- **LLM_Generator**: The subcomponent that produces self-contained *prose* (field descriptions, help text) as text completions via the Kiro_CLI. It does not produce code.
- **Agent_Rulebook**: The operator's InsightConnect plugin skills and steering files, referenced as Plugin_Agent resources. The authoritative statement of how a plugin is written; deliberately not duplicated inside the Plugin_Builder.
- **Quality_Gate**: The component that checks a plugin's hand-written code and reports located, actionable findings (syntax, formatting, lint, unit-test outcomes, coverage), excluding files the Insight_Plugin_CLI generates.
- **Repair_Loop**: The component that submits Quality_Gate findings to the Plugin_Agent for repair, re-checks, and repeats until the findings are resolved or it can no longer make progress. Its termination decision is deterministic.
- **Finding**: One located, actionable defect, carrying a stable key so the same defect is recognizable across Repair_Loop rounds.
- **Reference_Material**: Vendor API documentation or an API specification supplied by the user, staged in the Project_Folder for the Plugin_Agent to read.
- **Environment_Guard**: The default-deny construction of the environment passed to a delegated subprocess, admitting only a fixed base set plus that tool's own authentication variables.
- **Build_Prep**: The pre-build readiness step that resolves the current InsightConnect SDK version and the target Python interpreter from their authoritative sources, and reports which required tools are installed.
- **Spec_Completeness**: The check for the spec fields and conventions the InsightConnect toolchain requires, as distinct from structural schema conformance.
- **Definition_Of_Done**: The set of conditions in Requirement 27 that a generated plugin must satisfy before the Plugin_Builder may describe it as complete.
- **Spec_Validator**: The component that validates a `plugin.spec.yaml` against the InsightConnect plugin specification schema.
- **Code_Validator**: The component that lints, builds, validates, and runs tests against generated plugin code before export.
- **Build_Engine**: The component that packages a validated plugin into a `.plg` artifact.
- **Export_Manager**: The component that writes `.plg` files locally and/or uploads plugins to an InsightConnect tenant via the InsightConnect API.
- **Plugin_Registry**: The persistent store that records each created plugin, its metadata, its version, and its export history.
- **Credential_Store**: The component that securely stores and retrieves user-supplied secrets, including InsightConnect API keys.
- **Visualization_View**: The graphical representation of a plugin's structure and features shown to the user.
- **Documentation_Generator**: The component that produces plugin documentation (`help.md`) from the plugin definition.
- **Access_Controller**: The optional local access guard that, when enabled, requires a configured passphrase before a single operator can use the running instance of the Plugin_Builder.
- **Audit_Log**: The append-only record of security-relevant and export-relevant actions.
- **Cost_Controller**: The component that tracks and limits LLM token usage and request rate.
- **Plugin_Spec**: A `plugin.spec.yaml` file defining an InsightConnect plugin's name, title, description, version, vendor, connection, actions, triggers, tasks, and custom types.
- **PLG_Artifact**: A gzipped tarball (`.plg` file) containing a built InsightConnect plugin, suitable for import into a tenant.
- **InsightConnect_Tenant**: A customer's InsightConnect environment, addressed by a region base URL and authenticated with an API key.
- **Custom_Vendor_Suffix**: The literal string `_custom` appended to a plugin's vendor field.
- **Semantic_Version**: A version string in `MAJOR.MINOR.PATCH` format as required by the InsightConnect plugin specification.
- **Project_Folder**: A per-plugin directory on the local filesystem where the tool stores the plugin's spec, generated code, build artifacts, documentation, and version/export history for later lookup and reuse.
- **Kiro_CLI**: The command-line interface used as the primary LLM provider for content generation.
- **Insight_Plugin_CLI**: The insight-plugin command-line tool used to deterministically scaffold, refresh, and validate plugins.
- **Managed_Tooling**: The set of external tools and versions the Plugin_Builder depends on, comprising the Insight_Plugin_CLI, the InsightConnect SDK version pinned in each Plugin_Spec, the Kiro_CLI, and the plugin specification schema version.
- **Update_Manager**: The component that records installed Managed_Tooling versions, checks upstream sources for newer versions, notifies the user, and applies user-approved updates.
- **Production_Plugin_Source**: A configured local clone or remote location of the rapid7/insightconnect-plugins (public) or komand-plugins (private) repository from which an existing production plugin can be read.
- **Production_Plugin_Fork**: A read-only copy of a production plugin imported into a new Project_Folder as a custom plugin, carrying provenance metadata (source repository, original plugin name, original version) and a `_custom` vendor.
- **Provenance_Record**: Metadata recording the origin of a plugin draft: its entry mode (net-new, custom-iteration, or production-fork) and, for a fork, the source repository, original name, and original version.
## Requirements
### Requirement 1: Conversational Plugin Definition
**User Story:** As a plugin author, I want to describe the plugin I need in natural language through a chat interface, so that I can build a plugin without writing code or YAML by hand.
#### Acceptance Criteria
1. THE Conversation_Interface SHALL accept natural-language text input from the user with a length between 1 and 10,000 characters.
2. WHEN the user submits a plugin description containing at least 1 non-whitespace character, THE Plugin_Builder SHALL produce, within 30 seconds, a draft Plugin_Spec derived from that description that conforms to the plugin.spec.yaml structure.
3. WHEN the user submits a follow-up message that requests a change to the current draft, THE Plugin_Builder SHALL update the draft to incorporate the requested change while preserving all prior draft content not affected by the requested change.
4. WHEN a draft generation step completes, THE Conversation_Interface SHALL display the current state of the plugin draft.
5. IF the user's message cannot be interpreted as an actionable plugin instruction, THEN THE Conversation_Interface SHALL request clarification identifying the specific ambiguity.
6. IF the submitted input is empty or contains only whitespace, THEN THE Conversation_Interface SHALL reject the submission and display a message indicating that a non-empty description is required, and SHALL leave the current draft unchanged.
7. IF the Plugin_Builder fails to produce a draft Plugin_Spec from a valid description, THEN THE Conversation_Interface SHALL display an error message indicating that draft generation failed, and SHALL preserve the previous draft state without partial modification.
### Requirement 2: Extend Existing Plugins
**User Story:** As a plugin author, I want to import an existing plugin and modify it, so that I can extend plugins I have already built or obtained.
#### Acceptance Criteria
1. WHEN the user provides an existing PLG_Artifact that is a valid gzipped tarball containing a Plugin_Spec, THE Plugin_Builder SHALL extract its Plugin_Spec and code into an editable draft.
2. WHEN the user provides an existing Plugin_Spec file that conforms to the InsightConnect plugin specification schema, THE Plugin_Builder SHALL load it into an editable draft.
3. WHEN the user requests an addition to an imported plugin, THE Plugin_Builder SHALL add the requested action, trigger, task, or connection field, and SHALL retain all existing actions, triggers, tasks, connection fields, and code unchanged.
4. IF an imported artifact does not conform to the InsightConnect plugin specification schema, THEN THE Plugin_Builder SHALL report each nonconformity identifying the affected specification location and the violated schema rule, and SHALL NOT create an editable draft.
5. IF a provided PLG_Artifact cannot be extracted because it is not a valid gzipped tarball or does not contain a Plugin_Spec, THEN THE Plugin_Builder SHALL report an error indicating the extraction failure and SHALL NOT create an editable draft.
6. IF a provided Plugin_Spec file cannot be parsed, THEN THE Plugin_Builder SHALL report an error indicating the parse failure and SHALL NOT create an editable draft.
### Requirement 3: Deterministic Scaffolding and Delegated Implementation
**User Story:** As a plugin author, I want everything mechanical produced mechanically and everything else implemented by an agent that can check its own work, so that the plugin is correct and my LLM spend stays visible.

> **Revised.** The original version of this requirement restricted LLM use to
> three content types -- action logic, field descriptions, help text -- produced
> as text completions. That is what made the output unusable. Plugin source is
> not three independent snippets: the action bodies call the API client, the
> connection constructs it, the tests mock it, and correctness can only be
> established by running the toolchain over the result. Asking for a snippet and
> splicing the reply into a file produced invalid Python, because chat output is
> a transcript rather than a payload. Implementation is now delegated to an agent
> with tools and a working directory. Criterion 3.1 (deterministic scaffolding,
> zero LLM) was correct and is unchanged.

#### Acceptance Criteria
1. THE Generation_Engine SHALL produce the plugin directory structure, spec skeleton, and boilerplate files using the Deterministic_Scaffolder, which invokes the Insight_Plugin_CLI create and refresh operations, and SHALL make zero LLM invocations while producing these artifacts.
2. THE Generation_Engine SHALL produce a net-new plugin's working tree with the Insight_Plugin_CLI create operation, invoked from the parent directory of the plugin, and SHALL NOT substitute a refresh of a directory containing only a Plugin_Spec, because the two operations produce different package prefixes from identical input.
3. IF the Insight_Plugin_CLI declines to scaffold, THEN THE Generation_Engine SHALL treat the operation as failed even when the CLI exits with status zero, determined by whether the working tree was produced.
4. THE Generation_Engine SHALL delegate production of the plugin's hand-written source -- connection, API client, action, trigger and task logic, and unit tests -- to the Plugin_Agent, running with the plugin's Project_Folder as its working directory.
5. THE Plugin_Builder SHALL NOT parse plugin source code out of the Plugin_Agent's output stream, and SHALL NOT insert, splice, or re-indent agent output into an existing file.
6. THE Plugin_Builder SHALL determine which files a Plugin_Agent invocation changed by comparing the Project_Folder contents before and after the invocation, and SHALL NOT rely on the agent's own description of what it changed.
7. THE Generation_Engine SHALL restrict LLM_Generator invocations to self-contained prose content -- field descriptions and help text -- and SHALL NOT invoke the LLM_Generator to produce plugin source code.
8. WHERE a requested prose artifact matches an available template, THE Generation_Engine SHALL produce the artifact from that template and SHALL make zero LLM_Generator invocations for that artifact.
9. WHEN a Plugin_Agent or LLM_Generator invocation completes, THE Cost_Controller SHALL record the usage consumed by that invocation and SHALL add it to the cumulative session total.
10. WHEN a generation step completes, THE Plugin_Builder SHALL display to the user the cumulative session token total as a non-negative integer reflecting the sum of all recorded successful invocations in the current session.
11. WHERE the Kiro_CLI reports a usage figure it measured directly, THE Plugin_Builder SHALL record and display that figure in addition to the token total, and SHALL indicate that the token total is an estimate.
12. IF no usage figure was reported for an invocation, THEN THE Plugin_Builder SHALL represent the measured usage as unreported rather than as zero.
13. IF a Plugin_Agent or LLM_Generator invocation fails, THEN THE Generation_Engine SHALL halt the affected generation step, report the failure together with the invoked command's error output, and exclude the failed invocation from the cumulative session total.
### Requirement 4: LLM Usage Cost Controls
**User Story:** As an operator, I want configurable limits on LLM usage, so that a single session cannot incur unbounded cost.
#### Acceptance Criteria
1. THE Cost_Controller SHALL enforce a configurable maximum token budget per session, accepting integer values from 1 to 10,000,000 tokens.
2. IF a session's cumulative token usage reaches its configured token budget, THEN THE Cost_Controller SHALL block all subsequent LLM_Generator invocations for that session AND retain the session's already-completed output without persisting any partial result from the blocked invocation.
3. WHEN the Cost_Controller blocks an LLM_Generator invocation because the session token budget is reached, THE Cost_Controller SHALL return a message to the user indicating that the session token budget has been reached.
4. THE Cost_Controller SHALL enforce a configurable maximum number of LLM_Generator requests per minute per user, accepting integer values from 1 to 1,000 requests per minute.
5. IF a user's LLM_Generator request rate exceeds the configured maximum requests per minute, THEN THE Cost_Controller SHALL reject the additional request without invoking the LLM_Generator AND return a message to the user indicating that the request rate limit has been exceeded and the number of seconds after which further requests will be accepted.
6. WHERE no token budget is configured for a session, THE Cost_Controller SHALL apply a default maximum token budget of 100,000 tokens per session.
### Requirement 5: Visual Representation of the Plugin
**User Story:** As a plugin author, I want to see a graphical representation of my plugin's structure, so that I can understand and verify its components at a glance.
#### Acceptance Criteria
1. THE Visualization_View SHALL display the connection, actions, triggers, and tasks defined in the current plugin draft.
2. THE Visualization_View SHALL display the input schema and output schema for each action and trigger.
3. WHEN the plugin draft changes, THE Visualization_View SHALL update within 2 seconds to reflect the current plugin draft.
4. WHEN the user selects a single component in the Visualization_View, THE Visualization_View SHALL display that selected component's detailed fields.
5. IF the plugin draft contains no connection, no actions, no triggers, and no tasks, THEN THE Visualization_View SHALL display an empty-state indication rather than a blank view.
6. IF the plugin draft cannot be parsed, THEN THE Visualization_View SHALL display an error indication identifying the parse failure and SHALL retain the most recently rendered valid visualization.
### Requirement 6: Automated Documentation Generation
**User Story:** As a plugin author, I want documentation generated automatically from my plugin definition, so that the plugin ships with usage instructions without manual writing.
#### Acceptance Criteria
1. WHEN a plugin draft is generated or updated, THE Documentation_Generator SHALL produce a `help.md` document containing a distinct section for the plugin's connection, actions, triggers, and tasks.
2. WHEN the `help.md` document is produced, THE Documentation_Generator SHALL include, for each action and trigger, every input and output field with its name, data type, and required-or-optional status.
3. WHEN the `help.md` document is produced, THE Documentation_Generator SHALL include the plugin's title, description, version, and vendor.
4. IF a plugin component category (connection, actions, triggers, or tasks) contains zero defined items, THEN THE Documentation_Generator SHALL render that section's heading followed by a placeholder statement indicating that no items are defined, rather than omitting the section.
5. IF `help.md` generation fails because required plugin definition data (title, description, version, or vendor) is absent, THEN THE Documentation_Generator SHALL abort generation, leave any existing `help.md` unchanged, and return an error indicating which required field is missing.
### Requirement 7: Plugin Specification Validation
**User Story:** As a plugin author, I want the generated spec validated against the InsightConnect specification, so that my plugin will not be rejected on import.
#### Acceptance Criteria
1. WHEN a Plugin_Spec is generated or updated, THE Spec_Validator SHALL validate the Plugin_Spec against the InsightConnect plugin specification schema and complete validation within 5 seconds.
2. IF the Plugin_Spec fails schema validation, THEN THE Spec_Validator SHALL report every detected validation error to the user, each error including the field location (the path to the offending field within the Plugin_Spec) and a description of the violation.
3. WHEN the Spec_Validator validates the Plugin_Spec, THE Spec_Validator SHALL verify that the version field conforms to the Semantic_Version format (MAJOR.MINOR.PATCH).
4. IF the Plugin_Spec has failed schema validation, THEN THE Plugin_Builder SHALL prevent export of the plugin and indicate to the user that export is blocked because validation errors remain unresolved.
5. IF the version field does not conform to the Semantic_Version format, THEN THE Spec_Validator SHALL report a validation error identifying the version field as invalid and stating the expected MAJOR.MINOR.PATCH format.
6. WHEN the Plugin_Spec passes schema validation with no errors, THE Spec_Validator SHALL indicate validation success to the user.
### Requirement 8: Generated Code Validation
**User Story:** As a plugin author, I want generated code validated, linted, built, and tested before export, so that I do not ship a plugin that fails to run.

> **Revised.** Two things needed saying that this requirement did not say.
>
> First, the **four stages are the export gate, and only the export gate**. The
> checks that actually run against a plugin are a larger set -- parse, formatting,
> lint, unit tests, coverage, spec completeness -- and the earlier wording read as
> though the four stages were the whole validation surface. They are not. The rest
> belongs to the Quality_Gate (Requirement 26) and the Definition_Of_Done
> (Requirement 27), which are **advisory**: they inform the operator and drive
> repair, and they do not decide whether an export may proceed. Clearing the four
> stages, of which the Insight_Plugin_CLI validate operation is one, is what
> permits export.
>
> Second, the validate stage cannot run the validate operation *as the CLI invokes
> it*. See 8.9 and 8.10: one validator compares the plugin against its previous
> version in the `insightconnect-plugins` git remote, and raises an unhandled
> exception for a plugin that is not in a clone of that repository. The exception
> aborts the run, so the validators after it never execute and the failures already
> collected are never reported. Measured on a real generated plugin: 33 of 41
> validators ran, and eleven genuine failures were reported as nothing but a stack
> trace. Naming that validator as excluded is what makes the stage measurable.
>
> This requirement also remains insufficient on its own -- recording four failures
> and stopping is what allowed unusable plugins through. Requirement 26 adds the
> corrective step that acts on findings while the plugin is still being built.

#### Acceptance Criteria
1. WHEN the user requests a build, THE Code_Validator SHALL run static lint checks against the generated plugin code and record a pass or fail result for the lint stage.
2. WHEN the user requests a build, THE Code_Validator SHALL build the plugin container image defined by the plugin's Dockerfile and record a pass or fail result for the build stage.
3. WHEN the user requests a build, THE Code_Validator SHALL run the plugin's unit tests and record a pass or fail result for the test stage.
4. WHEN the user requests a build, THE Code_Validator SHALL run the Insight_Plugin_CLI's validators against the plugin, excluding those named per 8.9, and record a pass or fail result for the validate stage.
5. IF the lint stage, build stage, test stage, or validate stage records a fail result, THEN THE Code_Validator SHALL report failure details to the user identifying which stage failed and the associated error output.
6. IF the lint stage, build stage, test stage, or validate stage records a fail result, THEN THE Plugin_Builder SHALL prevent export of the plugin and retain the generated code unchanged.
7. WHEN the lint stage, build stage, test stage, and validate stage all record a pass result, THE Plugin_Builder SHALL permit export of the plugin.
8. IF the build stage or test stage runs longer than 600 seconds, THEN THE Code_Validator SHALL abort that stage, record a fail result for it, and report a timeout failure to the user.
9. THE Plugin_Builder SHALL maintain a named list of excluded validators, SHALL exclude a validator only where it cannot be satisfied by a plugin developed outside the `insightconnect-plugins` repository, and SHALL record for each one the reason it cannot apply.
10. WHERE a validator is excluded because it performs a check this tool performs itself, THE Plugin_Builder SHALL perform that check.
11. THE Code_Validator SHALL report how many validators ran, how many were available, and which were excluded, so that a pass is never mistaken for a run in which most validators were skipped.
12. THE Code_Validator SHALL derive the validate stage's pass or fail result from the Insight_Plugin_CLI's own validators and SHALL NOT reimplement any validator's check.
### Requirement 9: Build to PLG Artifact
**User Story:** As a plugin author, I want to build my plugin into a `.plg` file, so that I can import it into an InsightConnect tenant offline.
#### Acceptance Criteria
1. WHEN the user requests a local build and validation has passed, THE Build_Engine SHALL package the plugin into a single PLG_Artifact.
2. THE Build_Engine SHALL produce a PLG_Artifact that is a gzipped tarball containing the built plugin.
3. WHEN a PLG_Artifact is produced, THE Export_Manager SHALL place the PLG_Artifact in a user-accessible output location and display the location of the PLG_Artifact to the user.
4. IF the user requests a local build and validation has not passed, THEN THE Build_Engine SHALL not produce a PLG_Artifact and SHALL present an error indicating that validation failed.
5. IF packaging fails after validation has passed, THEN THE Build_Engine SHALL not produce a partial PLG_Artifact, SHALL leave the source plugin files unchanged, and SHALL present an error indicating that packaging failed.
### Requirement 10: Export to InsightConnect Tenant
**User Story:** As a plugin author, I want to push my plugin directly into my InsightConnect tenant, so that I can use it without a manual upload step.
#### Acceptance Criteria
1. WHERE the user provides InsightConnect_Tenant credentials, WHEN the user initiates a plugin export, THE Export_Manager SHALL upload the built plugin to the InsightConnect_Tenant using the InsightConnect API.
2. WHEN an upload to the InsightConnect_Tenant completes with a success response from the InsightConnect API, THE Export_Manager SHALL record the export in the Plugin_Registry, including the target tenant region base URL and the upload timestamp.
3. IF an upload to the InsightConnect_Tenant fails or does not receive a success response within 60 seconds, THEN THE Export_Manager SHALL report an error to the user indicating the upload failed, SHALL record the failed attempt in the Audit_Log, and SHALL leave the Plugin_Registry unchanged.
4. IF the user initiates an export without a non-empty tenant region base URL or without a non-empty API key, THEN THE Export_Manager SHALL reject the export before contacting the InsightConnect API and SHALL report an error indicating which credential is missing.
5. IF no built plugin artifact exists for the current plugin, THEN THE Export_Manager SHALL reject the export and SHALL report an error indicating that the plugin must be built before export.
### Requirement 11: Plugin Registry and Export History
**User Story:** As a plugin author, I want the tool to remember what it has created and exported, so that I can track my plugins and their versions over time.
#### Acceptance Criteria
1. WHEN a plugin is created, THE Plugin_Registry SHALL record the plugin name, vendor, version, and creation timestamp expressed in UTC.
2. WHEN a plugin is exported, THE Plugin_Registry SHALL record the exported version, the export target, and the export timestamp expressed in UTC.
3. THE Plugin_Registry SHALL retain the recorded plugin metadata and export history for each plugin across application restarts.
4. WHEN the user requests the history of a plugin that has one or more recorded entries, THE Plugin_Registry SHALL return the recorded versions and export events for that plugin ordered from most recent to oldest timestamp.
5. IF the user requests the history of a plugin that has no recorded entries, THEN THE Plugin_Registry SHALL return an empty history result without reporting an error.
6. IF recording plugin creation or export data fails, THEN THE Plugin_Registry SHALL return an error indication to the user and preserve any previously recorded history unchanged.
### Requirement 12: Automatic Version Bumping
**User Story:** As a plugin author, I want the tool to bump the version automatically when I re-export a plugin, choosing a major bump for breaking schema changes and a patch bump otherwise, so that tenant imports do not fail and consumers understand the impact of a change.
#### Acceptance Criteria
1. WHEN the user requests an export and the Plugin_Registry contains a prior export of the same plugin, THE Plugin_Builder SHALL compare the current Plugin_Spec against the most recently exported Plugin_Spec to determine whether a breaking schema change exists.
2. THE Plugin_Builder SHALL classify a change as a breaking schema change IF an existing action's or existing connection's input or output schema has a field removed, a field's type changed, or a previously optional field made required, or an existing action or connection is removed.
3. IF a breaking schema change exists relative to the most recently exported version, THEN THE Plugin_Builder SHALL increment the MAJOR segment of the Semantic_Version by 1 and reset the MINOR and PATCH segments to 0.
4. IF no breaking schema change exists and a prior export of the plugin exists at the current Semantic_Version, THEN THE Plugin_Builder SHALL increment the PATCH segment of the Semantic_Version by 1.
5. WHEN the version is incremented, THE Plugin_Builder SHALL set the resulting Semantic_Version so that it is strictly greater than every previously exported Semantic_Version of that plugin recorded in the Plugin_Registry.
6. WHEN the version is incremented, THE Plugin_Builder SHALL add a version_history entry describing the change and SHALL display the previous and new Semantic_Version to the user before the build begins.
7. WHERE no prior export of the plugin exists in the Plugin_Registry, THE Plugin_Builder SHALL export the plugin at its current Semantic_Version without incrementing.
8. IF the Plugin_Registry cannot be read to determine prior exported versions, THEN THE Plugin_Builder SHALL abort the export without building and display an error indicating the Plugin_Registry could not be accessed, leaving the Plugin_Spec version unchanged.
### Requirement 13: Custom Vendor Suffix
**User Story:** As a plugin author, I want a `_custom` suffix on the vendor field, so that my custom plugins are visually distinct from production Rapid7 plugins.
#### Acceptance Criteria
1. WHEN a Plugin_Spec is generated, THE Plugin_Builder SHALL append the Custom_Vendor_Suffix (the literal string "_custom") to the end of the existing vendor field value with no separating characters between the original value and the suffix.
2. IF a vendor value already ends with the Custom_Vendor_Suffix, evaluated as a case-sensitive exact match of the literal string "_custom", THEN THE Plugin_Builder SHALL leave the vendor value unchanged.
3. THE Plugin_Builder SHALL apply the Custom_Vendor_Suffix to the vendor field before the build and export steps begin, such that every exported Plugin_Spec's vendor field ends with the Custom_Vendor_Suffix.
4. IF the vendor field is empty, missing, or null when a Plugin_Spec is generated, THEN THE Plugin_Builder SHALL set the vendor field to the Custom_Vendor_Suffix value and continue processing without failing the build.
### Requirement 14: Credential and Secret Protection
**User Story:** As a security-conscious user running the tool locally, I want my InsightConnect API credentials stored encrypted on my machine and reused across sessions, so that I do not re-enter them each time while keeping them protected.
#### Acceptance Criteria
1. THE Credential_Store SHALL persist InsightConnect API credentials in encrypted form on the local filesystem, retaining no plaintext copy after the store operation completes.
2. WHEN the tool restarts, THE Credential_Store SHALL make previously persisted credentials available for reuse without requiring the user to re-enter them.
3. THE Plugin_Builder SHALL exclude API credentials and all other stored secrets from the Visualization_View, generated documentation, and PLG_Artifacts.
4. WHEN the Plugin_Builder displays or logs a stored secret, THE Plugin_Builder SHALL replace every character of the secret value with a fixed masking placeholder such that no character of the original secret value is visible.
5. WHEN the user requests deletion of a stored credential, THE Credential_Store SHALL remove the persisted credential and retain no plaintext or encrypted copy of it.
6. IF encryption of a credential fails during a store operation, THEN THE Credential_Store SHALL reject the store operation, retain no plaintext or partially stored credential, and return an error indicating the credential could not be stored.
### Requirement 15: Iterative Refinement Loop
**User Story:** As a plugin author, I want to refine the plugin over multiple turns of conversation, so that I can reach the result I want incrementally.
#### Acceptance Criteria
1. THE Plugin_Builder SHALL retain the current plugin draft, including all previously defined components and their attributes, across conversation turns within the same session.
2. WHEN the user requests a modification to a component identified by name that exists in the current draft, THE Plugin_Builder SHALL apply the requested change to that component and leave all other components in the draft unchanged.
3. WHEN the user requests removal of a component identified by name that exists in the current draft, THE Plugin_Builder SHALL remove only that component from the draft and leave all other components unchanged.
4. IF the user requests a modification or removal of a component whose name does not match any component in the current draft, THEN THE Plugin_Builder SHALL reject the request, return a message indicating that the named component was not found, and leave the draft unchanged.
### Requirement 16: Preview and Diff Before Export
**User Story:** As a plugin author, I want to review a preview and a diff of changes before export, so that I can confirm the plugin is correct before it leaves the tool.
#### Acceptance Criteria
1. WHEN the user requests an export, THE Plugin_Builder SHALL display a preview of the Plugin_Spec before performing the export.
2. WHEN the user requests an export, THE Plugin_Builder SHALL display the list of files that will be included in the PLG_Artifact before performing the export.
3. WHERE a prior version of the plugin exists in the Plugin_Registry, THE Plugin_Builder SHALL display a diff between the prior version and the current draft that identifies added, removed, and modified files.
4. WHERE no prior version of the plugin exists in the Plugin_Registry, THE Plugin_Builder SHALL indicate that the current draft is the first version and SHALL present all files as additions.
5. THE Plugin_Builder SHALL require explicit user confirmation of the preview before performing an export.
6. IF the user declines or cancels the confirmation, THEN THE Plugin_Builder SHALL abort the export, produce no PLG_Artifact, and retain the current draft unchanged.
### Requirement 17: Local Access Protection
**User Story:** As an operator running the tool on my own infrastructure, I want an optional local access guard, so that I can prevent casual unauthorized use of the running instance without managing multiple user accounts.
#### Acceptance Criteria
1. WHERE local access protection is enabled in configuration, WHEN a user attempts to access the Plugin_Builder, THE Access_Controller SHALL require the configured local passphrase before granting access.
2. IF local access protection is enabled and an incorrect passphrase is provided, THEN THE Access_Controller SHALL deny access and SHALL NOT execute any protected function.
3. WHERE local access protection is disabled in configuration, THE Plugin_Builder SHALL grant access without prompting for a passphrase.
4. THE Plugin_Builder SHALL bind its network interface to a configurable address, defaulting to the local loopback interface.
### Requirement 18: Audit Logging
**User Story:** As an operator, I want an audit trail of security-relevant and export actions, so that I can review what the tool did and who did it.
#### Acceptance Criteria
1. WHEN a user successfully authenticates, THE Audit_Log SHALL record the authentication event with the user identity and a UTC timestamp with at least second-level precision.
2. WHEN a plugin is built, THE Audit_Log SHALL record the build action, the plugin name, the plugin version, and a UTC timestamp with at least second-level precision.
3. WHEN credentials are stored or used for an upload, THE Audit_Log SHALL record the event with a UTC timestamp with at least second-level precision and with the secret value masked such that no character of the secret value appears in the recorded entry.
4. THE Audit_Log SHALL store records in append-only form and SHALL retain each record for a minimum of 90 days.
5. IF a user authentication attempt fails, THEN THE Audit_Log SHALL record the failed authentication event with the attempted user identity, the failure reason, and a UTC timestamp with at least second-level precision.
6. WHEN a plugin is exported, THE Audit_Log SHALL record the export action, the plugin name, the plugin version, and a UTC timestamp with at least second-level precision.
7. IF an attempt is made to alter or delete a previously written Audit_Log record, THEN THE Audit_Log SHALL reject the attempt and SHALL preserve the original record unchanged.
### Requirement 19: Build and Export Error Handling
**User Story:** As a plugin author, I want clear handling when a build or import fails, so that I can understand and fix the problem.
#### Acceptance Criteria
1. IF a build step fails, THEN THE Plugin_Builder SHALL halt the build and display, within 5 seconds of the failure, the name of the failing step and the complete error output emitted by that step.
2. IF an export to a tenant fails, THEN THE Plugin_Builder SHALL retain the built PLG_Artifact for at least 24 hours and make it available for the user to retry the export or download the artifact.
3. WHEN a build failure or an export failure occurs, THE Plugin_Builder SHALL leave the current plugin draft unchanged, retaining all source files and configuration exactly as they were before the build or export was initiated.
4. IF a build or export fails, THEN THE Plugin_Builder SHALL present a failure indication to the user that distinguishes a build failure from an export failure.
5. WHERE the error output for a failing build step exceeds 10,000 characters, THE Plugin_Builder SHALL display the first 10,000 characters and provide access to the full error output.
### Requirement 20: Local Deployment and Configuration
**User Story:** As a user, I want to download the tool and run it locally or on my own infrastructure, so that I control where it runs and what it can reach.
#### Acceptance Criteria
1. THE Plugin_Builder SHALL run as a self-contained application that a user can start on a local machine or self-managed infrastructure without a hosted backend service.
2. WHEN the Plugin_Builder starts, THE Plugin_Builder SHALL read its LLM provider, token budget, rate-limit, network bind address, and access-protection settings from local configuration.
3. THE Plugin_Builder SHALL use the Kiro_CLI both as the Plugin_Agent that implements plugin source and as the LLM provider for prose content generation.
4. WHERE tenant API access is unavailable, THE Plugin_Builder SHALL support local build and PLG_Artifact download without requiring an upload to a tenant.
5. IF the Kiro_CLI is not available or not authenticated at startup, THEN THE Plugin_Builder SHALL report an error indicating the Kiro_CLI could not be used and SHALL identify the remediation step.
6. IF any required configuration setting is missing or invalid at startup, THEN THE Plugin_Builder SHALL halt startup and emit an error indicating which configuration setting is missing or invalid.
7. THE Plugin_Builder SHALL read the Agent_Rulebook from the operator's installed skills and steering, and SHALL NOT maintain its own copy of the plugin-authoring rules those files define.
8. IF a file referenced by the Agent_Rulebook is not installed, THEN THE Plugin_Builder SHALL continue with the remaining files and report that the Plugin_Agent is operating with reduced guidance.
### Requirement 21: Project-Folder History and Reuse
**User Story:** As a plugin author, I want each plugin's work saved in its own project folder with history, so that I can look up, reuse, and resume prior builds later.
#### Acceptance Criteria
1. WHEN a plugin is created, THE Plugin_Builder SHALL create a Project_Folder for that plugin on the local filesystem.
2. WHEN a plugin is generated, built, or exported, THE Plugin_Builder SHALL store the current Plugin_Spec, generated code, documentation, and build artifacts in that plugin's Project_Folder.
3. THE Plugin_Builder SHALL retain, within the Project_Folder, a record of each prior version including its Plugin_Spec and export outcome.
4. WHEN the user requests the list of previously created plugins, THE Plugin_Builder SHALL return each plugin recorded in a Project_Folder with its name, current version, and last modification timestamp.
5. WHEN the user selects a previously created plugin, THE Plugin_Builder SHALL load that plugin's most recent Plugin_Spec and code from its Project_Folder into an editable draft.
6. IF a Project_Folder is missing required content or cannot be read when loading a plugin, THEN THE Plugin_Builder SHALL report the specific missing or unreadable content and SHALL NOT create a partial draft.
### Requirement 22: Natural-Language Iteration on a Prior Build
**User Story:** As a plugin author, I want to load a previous build and describe enhancements or bug fixes in natural language, so that I can evolve an existing plugin without starting over.
#### Acceptance Criteria
1. WHEN the user loads a previously created plugin and submits a natural-language enhancement request, THE Plugin_Builder SHALL apply the requested addition or change to the loaded draft while preserving all unaffected components.
2. WHEN the user loads a previously created plugin and submits a natural-language bug-fix request identifying a defect, THE Plugin_Builder SHALL modify the affected code or Plugin_Spec to address the described defect while preserving unaffected components.
3. WHEN the tool modifies a loaded plugin's Plugin_Spec by adding or changing an action, trigger, task, or connection, THE Plugin_Builder SHALL invoke the Insight_Plugin_CLI refresh operation to regenerate derived scaffolding rather than editing generated files by hand.
4. WHEN an iteration changes a loaded plugin, THE Plugin_Builder SHALL re-run Spec_Validator and Code_Validator before permitting export.
5. IF a natural-language iteration request cannot be mapped to a specific component or change, THEN THE Plugin_Builder SHALL request clarification identifying the ambiguity and SHALL leave the loaded draft unchanged.
### Requirement 23: Tooling Update Management
**User Story:** As a user, I want to know when newer versions of the tool's dependencies are available and to apply them on demand, so that my plugins are built against current, import-compatible tooling without unexpected changes.
#### Acceptance Criteria
1. WHEN the Plugin_Builder starts, THE Update_Manager SHALL record the currently installed version of each Managed_Tooling component.
2. WHEN a plugin is built, THE Plugin_Builder SHALL store the Insight_Plugin_CLI version and the InsightConnect SDK version used for that build in the plugin's Project_Folder.
3. WHEN the Plugin_Builder starts and at a configurable interval thereafter, THE Update_Manager SHALL check upstream sources for the latest available version of each Managed_Tooling component without blocking the Conversation_Interface.
4. THE Update_Manager SHALL cache the result of an update check for a configurable duration and SHALL NOT perform a new upstream check until the cached result expires.
5. WHERE network access is unavailable or offline mode is enabled in configuration, THE Update_Manager SHALL skip upstream update checks and SHALL continue operating using the installed Managed_Tooling versions.
6. IF a newer version of any Managed_Tooling component is available, THEN THE Update_Manager SHALL notify the user of the component, the installed version, the available version, and a reference to the version's changelog.
7. THE Update_Manager SHALL NOT upgrade any Managed_Tooling component without explicit user approval.
8. WHEN the user approves an update, THE Update_Manager SHALL install the selected version, run a smoke test that validates a known-good sample plugin with the updated tooling, and record the new installed version only if the smoke test passes.
9. IF the smoke test fails after installing an update, THEN THE Update_Manager SHALL roll back to the previously installed version and report that the update was not applied and why.
10. WHERE a loaded plugin's pinned InsightConnect SDK version is behind the latest known-good SDK version, THE Plugin_Builder SHALL offer to update the plugin's SDK version during the next refresh, and SHALL leave the pinned version unchanged unless the user approves the update.
### Requirement 24: Plugin Entry Mode Selection
**User Story:** As a plugin author, I want to choose at the start whether I am creating a net-new plugin, iterating on a previously created custom plugin, or enhancing an existing production plugin, so that the tool loads the right starting point.
#### Acceptance Criteria
1. THE Conversation_Interface SHALL present three entry modes: create a net-new plugin, iterate on a previously created custom plugin, and enhance an existing production plugin.
2. WHEN the user selects create a net-new plugin, THE Plugin_Builder SHALL begin with an empty draft.
3. WHEN the user selects iterate on a previously created custom plugin, THE Plugin_Builder SHALL present the list of plugins recorded in Project_Folders for selection and load the chosen plugin into an editable draft.
4. WHEN the user selects enhance an existing production plugin, THE Plugin_Builder SHALL prompt the user to select a Production_Plugin_Source and a plugin within it.
5. WHEN a plugin draft is created through any entry mode, THE Plugin_Builder SHALL record a Provenance_Record identifying the entry mode used.
### Requirement 25: Enhance Existing Production Plugin
**User Story:** As a plugin author, I want to import a production plugin from the public or private repository and enhance it as a custom plugin, so that I can extend production functionality without altering or colliding with the production plugin.
#### Acceptance Criteria
1. THE Plugin_Builder SHALL read production plugins from a user-configured local clone of a Production_Plugin_Source.
2. WHERE no local clone is configured or the requested plugin is absent locally, THE Plugin_Builder SHALL fetch the plugin from the remote Production_Plugin_Source, using stored git credentials for the private repository.
3. WHEN the user selects a production plugin, THE Plugin_Builder SHALL copy the plugin into a new Project_Folder and SHALL NOT modify any file in the Production_Plugin_Source.
4. WHEN a production plugin is imported for enhancement, THE Plugin_Builder SHALL apply the Custom_Vendor_Suffix to the vendor field, retain the original plugin name, and record a Provenance_Record containing the source repository, the original plugin name, and the original version.
5. WHEN a production plugin is imported for enhancement, THE Plugin_Builder SHALL preserve the original license and attribution references in the plugin's resources.
6. WHEN a plugin is imported from the private repository, THE Plugin_Builder SHALL display a notice that the source is private and subject to its usage restrictions.
7. THE Plugin_Builder SHALL import production plugins that use either the current `icon_` package prefix or the legacy `komand_` package prefix.
8. WHEN the user requests a diff for an enhanced production fork, THE Plugin_Builder SHALL display the differences between the current draft and the original production baseline.
9. IF a required git credential for the private repository is missing when a remote fetch is attempted, THEN THE Plugin_Builder SHALL reject the fetch and report that the git credential is required.
10. IF a selected production plugin cannot be read or does not conform to the InsightConnect plugin specification schema, THEN THE Plugin_Builder SHALL report the specific error and SHALL NOT create a partial draft.

### Requirement 26: Corrective Validation
**User Story:** As a plugin author, I want the tool to fix what its own checks find, so that I am not handed a list of defects to repair by hand.

> **New.** The original specification required the four-stage pipeline to record
> results and block export. Nothing was required to *act* on those results, and
> nothing did: a plugin with four unparseable action files was reported as built
> because every stage had run and recorded its outcome. Reporting is not
> repairing.

#### Acceptance Criteria
1. THE Quality_Gate SHALL check the plugin's hand-written code and produce a Finding for each defect, each Finding identifying the file, the location within it where one applies, a defect code, and a description.
2. THE Quality_Gate SHALL check, at minimum: that every hand-written Python file parses; that formatting matches the formatter the Insight_Plugin_CLI applies; the linter named by the Agent_Rulebook; that the plugin's unit tests pass; and statement coverage of the plugin package.
3. THE Quality_Gate SHALL exclude files generated by the Insight_Plugin_CLI from its Findings, because the Agent_Rulebook forbids editing them and a Finding against one is not actionable.
4. IF a Quality_Gate check cannot run because its tool is unavailable, THEN THE Quality_Gate SHALL report that check as skipped and SHALL NOT report it as passed.
5. WHEN the Quality_Gate produces one or more Findings, THE Repair_Loop SHALL submit them to the Plugin_Agent for repair and SHALL re-run the Quality_Gate afterwards.
6. THE Repair_Loop SHALL decide whether to continue by comparing Finding keys between rounds, and SHALL NOT delegate that decision to a Plugin_Agent or LLM_Generator.
7. IF a round resolves no Finding present in the previous round, THEN THE Repair_Loop SHALL stop and report that it made no progress.
8. IF the Repair_Loop reaches its configured maximum number of rounds with Findings outstanding, THEN THE Repair_Loop SHALL stop and report that it reached the limit, and SHALL NOT report the outcome as successful.
9. THE Repair_Loop SHALL derive whether any Findings remain from the Findings themselves and not from which stopping condition applied, so that a stalled or limit-reached outcome cannot be represented as complete.
10. THE Repair_Loop SHALL treat a Finding whose location has moved by less than a bounded distance within the same file, with the same defect code, as the same Finding, so that repairs which shift surrounding lines do not appear as new defects.
11. THE Repair_Loop SHALL treat two Findings of the same defect code in the same file at distinct locations as distinct Findings.
12. WHEN the Repair_Loop asks the Plugin_Agent to repair Findings, THE Plugin_Builder SHALL instruct it not to edit files generated by the Insight_Plugin_CLI.

### Requirement 27: Definition of Done for a Generated Plugin
**User Story:** As a plugin author, I want the tool to tell me the plugin is finished only when it actually is, so that "done" means I can import and run it.

> **New, then revised.** The original specification contained no requirement that
> a generated plugin run. It required a schema-valid spec and recorded stage
> results, both of which were delivered while the plugin was unusable. This
> requirement states the conditions explicitly so they can be checked rather than
> assumed.
>
> **The Definition_Of_Done is advisory (27.6).** It reports; it does not gate.
> Export permission is Requirement 8's four-stage conjunction, of which the
> Insight_Plugin_CLI validate operation is one -- a plugin that clears those stages
> may be exported even with Definition_Of_Done conditions outstanding. This is a
> deliberate decision, not an oversight. The two answer different questions: the
> gate answers "will this import and run", the definition of done answers "is this
> finished to the standard the project sets". An operator is entitled to ship
> something that works while knowing its coverage is thin. What they are not
> entitled to is *not being told* -- hence 27.2 and 27.3, which are unaffected.

#### Acceptance Criteria
1. THE Plugin_Builder SHALL treat a plugin as complete only when all of the following hold: the Insight_Plugin_CLI validate operation passes; the Agent_Rulebook's linter reports no findings against hand-written code; every hand-written Python file parses; the plugin exposes an API client with centralized request handling and per-action methods; the connection's connect and test operations are implemented rather than stubbed; unit tests covering each action pass; statement coverage of the plugin package meets the configured minimum; and a dependency manifest exists.
2. IF any Definition_Of_Done condition is unmet, THEN THE Plugin_Builder SHALL report the plugin as incomplete and SHALL name each unmet condition.
3. THE Plugin_Builder SHALL NOT describe a plugin as complete, ready, or successful while any Definition_Of_Done condition is unmet.
4. THE Plugin_Builder SHALL determine each Definition_Of_Done condition by executing a check, and SHALL NOT infer any of them from a Plugin_Agent's report of its own work.
5. WHERE a Definition_Of_Done condition cannot be evaluated because a required tool is unavailable, THE Plugin_Builder SHALL report that condition as unverified rather than as met.
6. THE Definition_Of_Done SHALL be advisory: THE Plugin_Builder SHALL NOT block export solely because a Definition_Of_Done condition is unmet, and SHALL determine export permission per Requirement 8.7.
7. WHEN export is permitted while any Definition_Of_Done condition is unmet or unverified, THE Plugin_Builder SHALL present the outstanding conditions alongside the export preview, so that proceeding is an informed choice rather than an uninformed one.

### Requirement 28: Vendor Reference Material
**User Story:** As a plugin author building against a real vendor API, I want to supply its documentation and have the implementation use it, so that endpoints and payloads are correct rather than guessed.

> **New, then extended.** The Plugin_Agent has no means of retrieving vendor
> documentation itself. Without supplied reference material it infers endpoint
> paths, methods, and payload shapes, and inferred endpoints are wrong.
>
> The extension (28.8 onward) covers where reference material *comes from*. The
> original wording assumed the user always pastes a document, but a request is as
> likely to be "build me a plugin for vendor X" with a link, a PDF, or nothing at
> all. Three points decide the design:
>
> **The Plugin_Builder retrieves; the Plugin_Agent does not.** Fetched pages are
> untrusted content, and this specification already forbids putting untrusted
> content into the prompt of a shell-capable agent. Granting the agent network
> access to fetch documentation would place that same content inside the
> reasoning of a process that can execute commands. Retrieval therefore happens
> in the Plugin_Builder, which stores the result as a file; the agent's contract
> is unchanged -- it reads files (28.6).
>
> **There is no discovery.** The Plugin_Builder does not search for
> documentation from a vendor name. A bare name yields a request for a URL or an
> existing plugin to reference (28.12), because a guessed documentation source is
> the same defect as a guessed endpoint, one step earlier. It follows that every
> retrieval is of a location the user supplied, which is what authorizes it --
> there is no separate confirmation step for a URL the user has just given.
>
> **Traceable, not verified.** Recorded provenance and a required citation per
> action establish where an endpoint came from (28.9, 28.14). Neither establishes
> that the endpoint is *correct*; only calling the API would. The distinction is
> stated so that "sourced" is never read as "verified".

#### Acceptance Criteria
1. THE Conversation_Interface SHALL accept Reference_Material as an attachment alongside a natural-language message.
2. WHEN Reference_Material is supplied, THE Plugin_Builder SHALL write it unmodified into the plugin's Project_Folder in a location the Plugin_Agent can read.
3. THE Plugin_Builder SHALL store Reference_Material within the tool-owned metadata subtree so that it is excluded from the PLG_Artifact.
4. THE Plugin_Builder SHALL derive the stored filename such that a supplied name cannot cause a write outside the Reference_Material location.
5. WHEN delegating implementation for a plugin with Reference_Material, THE Plugin_Builder SHALL identify the stored files to the Plugin_Agent and instruct it to use them for endpoint paths, HTTP methods, request and response shapes, authentication, pagination, and error formats.
6. THE Plugin_Builder SHALL pass Reference_Material to the Plugin_Agent as files rather than as extracted or summarized content in a prompt.
7. IF Reference_Material cannot be written, THEN THE Plugin_Builder SHALL continue the delegation without it and report that it was unavailable, treating it as an aid rather than a precondition.
8. THE Plugin_Builder SHALL accept Reference_Material from any of: a supplied document, a supplied URL, or an existing plugin in a configured Production_Source.
9. THE Plugin_Builder SHALL record for each piece of Reference_Material its origin, the time it was obtained, its media type, its size, and a content hash, and SHALL retain that record alongside the stored file.
10. THE Plugin_Builder SHALL perform every retrieval itself and SHALL NOT grant the Plugin_Agent network access for the purpose of obtaining Reference_Material.
11. WHERE Reference_Material is supplied in a format the Plugin_Agent cannot read as text, THE Plugin_Builder SHALL extract its text, SHALL store the extracted text as the readable form, and SHALL record that the stored form is an extraction rather than the original.
12. IF a plugin is requested by vendor or product name with neither a supplied document, nor a URL, nor a matching plugin in a Production_Source, THEN THE Plugin_Builder SHALL request one of those before implementing, and SHALL NOT search for documentation nor infer endpoints from the name.
13. IF the user directs implementation to proceed without Reference_Material, THEN THE Plugin_Builder SHALL record that the plugin was implemented without it and SHALL report that as an unmet Definition_Of_Done condition.
14. WHEN delegating implementation for a plugin with Reference_Material, THE Plugin_Builder SHALL instruct the Plugin_Agent to record, for each action, which Reference_Material it took the endpoint and payload shapes from.
15. THE Plugin_Builder SHALL retrieve only over HTTPS, SHALL enforce a configured maximum response size and timeout, SHALL restrict retrieval to media types it can store as text, and SHALL NOT send credentials or session state with a retrieval.
16. IF a retrieval fails, is refused by 28.15, or returns content that cannot be stored as text, THEN THE Plugin_Builder SHALL report the reason and SHALL NOT substitute inferred content for it.
17. THE Plugin_Builder SHALL treat retrieved Reference_Material as untrusted data, SHALL NOT interpret instructions contained within it, and SHALL record its origin so that content influencing an implementation is attributable.

### Requirement 29: Delegated Execution Isolation
**User Story:** As a security-conscious operator, I want the delegated CLI to receive only what it needs, so that running it cannot expose my other credentials.

> **New.** The Plugin_Builder decrypts InsightConnect tenant API keys and private
> repository git credentials into its own process, and it launches a third-party
> CLI as a subprocess. A subprocess launched without an explicit environment
> inherits the parent's, which handed that CLI every secret in the operator's
> environment.

#### Acceptance Criteria
1. WHEN the Plugin_Builder launches a delegated CLI, THE Environment_Guard SHALL construct the subprocess environment by admitting only a fixed set of base variables plus the name prefixes that CLI requires to authenticate, and SHALL exclude every other variable.
2. THE Plugin_Builder SHALL NOT launch a delegated CLI with an inherited environment.
3. THE Plugin_Builder SHALL pass a delegated CLI's prompt on standard input and SHALL NOT pass it as a command-line argument.
4. THE Plugin_Builder SHALL grant the Plugin_Agent only the tools required to build a plugin, and SHALL enumerate the granted tools explicitly rather than granting all available tools.
5. IF a delegated CLI invocation fails, THEN THE Plugin_Builder SHALL surface the invocation's error output and SHALL NOT return silently.
6. THE Plugin_Builder SHALL NOT include content originating outside the tool -- imported production plugin source, PLG_Artifact contents, or Reference_Material -- in a prompt to a shell-capable agent.
7. WHERE the Environment_Guard withholds variables, THE Plugin_Builder SHALL be able to report the withheld variable names without reporting any value.

### Requirement 30: Spec Completeness and Build Readiness
**User Story:** As a plugin author, I want the spec to carry every field the toolchain requires with current versions, so that validation does not fail on missing metadata.

> **New.** Schema conformance and toolchain acceptance are different properties.
> Specs the tool produced were structurally valid and rejected by
> `insight-plugin validate` for absent metadata: no SDK block, no version
> history, no supported versions, no resource URLs, and no output examples.

#### Acceptance Criteria
1. THE Spec_Completeness check SHALL report each absent or empty spec field that the InsightConnect toolchain requires, each reported separately with its location.
2. THE Spec_Completeness check SHALL report each output field of an action, trigger, or task that carries no example value.
3. THE Spec_Completeness check SHALL report a connection field whose declared credential type is not one the InsightConnect platform defines.
4. THE Spec_Completeness check SHALL report spec text that the InsightConnect toolchain rejects or that produces invalid generated code.
5. THE Spec_Completeness check SHALL be reported separately from structural schema validation, so that absent metadata does not render an in-progress draft schema-invalid.
6. THE Build_Prep step SHALL resolve the InsightConnect SDK version from the SDK distribution's own changelog, and SHALL NOT use a version recorded in the Plugin_Builder.
7. WHERE the SDK changelog is unavailable, THE Build_Prep step SHALL fall back to the installed SDK version and SHALL report which source was used.
8. WHEN a Plugin_Spec carries no SDK version, THE Plugin_Builder SHALL record the resolved version into the spec before scaffolding, and SHALL leave an SDK version already present unchanged.
9. THE Build_Prep step SHALL resolve the target Python interpreter from the installed interpreter set rather than from a version recorded in the Plugin_Builder, and SHALL report when it falls back to an interpreter it cannot confirm.
10. THE Build_Prep step SHALL report which required external tools are installed.
