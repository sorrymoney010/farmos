# FARMOS Device Compute Platform - Requirements

## Goal
Build a system where old devices (phones, laptops, etc.) can earn money by providing compute power or being available for work — owned entirely by FARMOS, no external dependencies.

## Questions to clarify before building:

### 1. Device Landscape
- What devices are we targeting? Old Android phones? iPhones? Old laptops? Raspberry Pis?
- Do we already have devices, or are we recruiting/enrolling them?

### 2. What Compute Can These Devices Actually Provide?
- Phone CPUs are weak — can't do heavy ML training or large-scale compute
- Can do: lightweight API serving, web scraping, data processing, simple inference, monitoring checks
- What's realistic for old phones?

### 3. Monetization Model
- Option A: FARMOS sells device compute to customers (like Acurast does)
- Option B: FARMOS runs its own jobs on the devices (internal use)
- Option C: Devices earn "FARMOS credits" that can be redeemed later
- Option D: Devices earn real money (crypto, paypal, etc.)

### 4. Device Enrollment
- How do devices join the network?
- Do we need device attestation/verification? (like Acurast's TEE)
- Or simpler — just register and trust?

### 5. Job Execution
- What runs on devices? JavaScript? Python? Something else?
- How does FARMOS send jobs to devices?
- How do devices report results back?

### 6. Architecture
- FARMOS backend (job management, device registry, payments)
- Device agent (runs on each device, receives jobs, reports results)
- Dashboard (see devices, jobs, earnings)

### 7. MVP Scope
- One device running one type of job?
- Multiple devices?
- Multiple job types?

## What I'm thinking for MVP:

```
FARMOS Backend
├── Device Registry (register/enroll devices)
├── Job Queue (create/manage jobs)
├── Job Distribution (send jobs to devices)
├── Results Collector (receive results from devices)
└── Earnings Tracker (track what each device earned)

Device Agent (runs on each device)
├── Register with FARMOS
├── Poll for jobs / receive push
├── Execute job
└── Report result + earn credit

Dashboard
├── Device status (online/offline, earnings)
├── Job status (pending, running, completed)
└── Earnings overview
```

## Before I build, I need answers to:
1. What devices are we using? (Stratus C7? iPhone? Both? More?)
2. What jobs should these devices run?
3. How should devices get paid? (credits, real money, crypto?)
4. Do we need device verification/attestation?
5. What's the simplest version that proves this works?

