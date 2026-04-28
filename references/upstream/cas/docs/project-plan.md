# Project Plan

## Objective

Build a research grade repository for the CAS reserving proposal that supports implementation, benchmarking, governance controls, agent runtime portability, and final reporting without changing the proposal's core actuarial scope.

## Workstreams

### 1. Benchmark Package

- Define synthetic reserving case schema.
- Create scenario taxonomy for routine, anomaly, and stress cases.
- Add scoring rubrics for numeric consistency, explanation quality, and escalation quality.

### 2. Governed Workflow

- Model the agent governed workflow stages.
- Define a portable agent adapter interface for runtimes such as OpenClaw and Hermes.
- Define constitutional checks and review gates.
- Separate deterministic calculation from narrative generation.

### 3. Model and Runtime Trials

- Test common hosted model APIs for rapid comparison.
- Test local LLM deployment patterns that may fit company environments.
- Track workstation, GPU, local model serving, and OpenClaw or Hermes server needs.
- Preserve the option to test a stronger agent runtime if one becomes available during the project.

### 4. Evaluation Harness

- Implement baseline comparison paths.
- Capture repeatability metrics and audit artifacts.
- Produce experiment outputs suitable for the technical report.

### 5. Documentation and Reproducibility

- Maintain architecture and decision records.
- Track assumptions and implementation boundaries.
- Package reproducible examples for CAS review and future distribution, including example agent runtime materials.

## Delivery Phases

### Phase 0. Final Submission Readiness

- Keep the final proposal PDF and final deck PDF clearly separated from history files.
- Archive previous proposal materials so they do not interfere with submission.
- Align README, project plan, architecture, and development notes with the final CAS schedule.

### Phase 1. Kickoff and Specification

- Finalize benchmark schema.
- Draft Constitution v1.
- Define workflow interfaces, agent adapter boundaries, and artifact formats.
- Prepare initial OpenClaw and Hermes adapter notes.

### Phase 2. Beta Workflow

- Implement input intake model.
- Add deterministic reserving adapter boundary.
- Add narrative generation boundary and logging hooks.
- Add the first agent runtime implementation.
- Prepare portability checks for a second runtime.

### Phase 3. Interim Experimental Campaign

- Add hard constraints and review trigger logic.
- Add human approval checkpoints.
- Add baseline comparison runners.
- Run governed workflow experiments, ablations, repeatability checks, and expert review.
- Prepare the interim progress report and executive summary.

### Phase 4. Final Packaging

- Complete robustness checks.
- Prepare reproducibility bundle.
- Package portable agent workflow assets and example runtime notes.
- Resolve remaining portability issues.
- Draft final report materials.

### Phase 5. CAS Final Delivery

- Finalize the technical report.
- Finalize the executive summary and presentation deck.
- Package code, benchmark data, scoring rubrics, runtime notes, and reproducibility materials for CAS.

## Timeline Alignment

### Monday, April 27, 2026

- Proposal submission deadline.
- Final proposal and final deck are the submission files.

### Friday, June 12, 2026

- Researcher notification and project start.

### June 12 to June 30, 2026

- Confirm reserving use case.
- Draft Actuarial Constitution v1.
- Specify agent neutral workflow stages and adapter interface.
- Generate the first synthetic loss triangle benchmark cases.
- Configure initial OpenClaw and Hermes example environments where feasible.

### July 2026

- Implement deterministic reserving adapters, hard constraint checks, review gates, and audit logging.
- Run baseline evaluations.
- Test the workflow through at least one agent runtime and prepare a portability check for the second.
- Begin hosted model API and local LLM trial setup.

### August 1 to August 28, 2026

- Execute main experiments across governed workflow, baselines, ablations, repeatability checks, and scoped model adaptation.
- Conduct expert review of triggered cases.
- Complete interim progress report and executive summary by Friday, August 28, 2026.

### August 31 to September 25, 2026

- Complete robustness checks.
- Refine the portable workflow specification.
- Package benchmark cases, scoring rubrics, example OpenClaw/Hermes materials, and reproducibility assets.
- Resolve portability, server cost, and local model serving notes.

### September 28 to October 2, 2026

- Finalize technical report, executive summary, presentation materials, code package, and distribution materials.
- Submit final paper and deliverables by Friday, October 2, 2026.

## Definition of Done

The repository is ready to support the CAS project when:

- the final submission files are clearly separated from historical proposal materials,
- the implementation areas are clearly separated by concern,
- the architecture and project plan match the CAS calendar,
- hosted API and local LLM testing assumptions are documented,
- future contributors can build benchmark, workflow, agent adapter, and evaluation code without restructuring the repository.
