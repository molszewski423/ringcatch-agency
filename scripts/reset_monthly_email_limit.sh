#!/bin/bash
# Run on the 1st of each month to reset EMAIL_DAILY_LIMIT to the monthly spread
# Total capacity: Brevo 4030 + Resend 3000 = 7030/month
DAILY=234
sed -i "s/EMAIL_DAILY_LIMIT=.*/EMAIL_DAILY_LIMIT=$DAILY/" /home/mike/agency/.env
ssh mike@100.97.45.57 "kubectl rollout restart deployment/agency-outreach -n agency"
echo "$(date): EMAIL_DAILY_LIMIT reset to $DAILY" >> /home/mike/agency/scripts/email_limit.log
