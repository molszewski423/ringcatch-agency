# SOP: Client Onboarding

## Trigger
Client pays setup fee ($450) via Stripe. agency-billing receives webhook, fires PAYMENT_RECEIVED event.

## Step 1: Intake (Day 0, automated)
agency-delivery generates:
- Botpress chatbot JSON (customized for client niche)
- PDF onboarding guide (ReportLab)
- Loom video script for handoff

agency-success sends welcome email with:
- Link to onboarding guide PDF
- Calendar link to schedule setup call (Cal.com)
- What to expect in the first 7 days

## Step 2: Setup Call (Day 1-3)
Collect from client:
- Business name, tagline, services offered
- Hours of operation
- Top 10 FAQs (ask them to write these out)
- Preferred contact method for leads (email/SMS/phone)
- Any existing booking system (Calendly, Google Calendar, etc.)
- Brand colors and logo (optional, for chat widget styling)

## Step 3: Build & Deploy (Day 3-5)
- Customize Botpress JSON with client info
- Configure webhook to client's preferred lead notification
- Embed code snippet for client's website
- Test all conversation flows end-to-end
- QA: test on mobile, desktop, and after-hours scenarios

## Step 4: Handoff (Day 5-7)
- Send embed code + installation instructions
- Record Loom walkthrough of what was built
- Schedule Day-7 check-in call
- Mark delivery as complete in deliveries table
- agency-success schedules testimonial request for Day 30

## Step 5: Ongoing (Month 1+)
- Monthly check-in email from agency-success
- Churn risk monitoring (chatbot conversation count)
- High churn risk alert if <5 conversations in 30 days
- Upsell check at Month 3 (multi-location, SMS, CRM)
