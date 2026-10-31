#!/bin/sh
set -eu
umask 077

printf '%s\n' "$ENVOY_BOOTSTRAP" > /etc/envoy/envoy.yaml

printf '%s\n' "$ENVOY_CLUSTERS" | sed "s|\${SERVICE_NAMESPACE}|${SERVICE_NAMESPACE}|g" > /etc/envoy/cds.yaml
echo "Generating Envoy configuration..."

printf '%s\n' "$ENVOY_LISTENERS" | sed -e "s|\${ANON_KEY}|${ANON_KEY}|g" \
    -e "s|\${ANON_KEY_ASYMMETRIC}|${ANON_KEY_ASYMMETRIC}|g" \
    -e "s|\${SERVICE_ROLE_KEY}|${SERVICE_ROLE_KEY}|g" \
    -e "s|\${SERVICE_ROLE_KEY_ASYMMETRIC}|${SERVICE_ROLE_KEY_ASYMMETRIC}|g" \
    -e "s|\${SUPABASE_PUBLISHABLE_KEY}|${SUPABASE_PUBLISHABLE_KEY}|g" \
    -e "s|\${SUPABASE_SECRET_KEY}|${SUPABASE_SECRET_KEY}|g" \
    > /etc/envoy/lds.yaml

if [ -n "$SUPABASE_SECRET_KEY" ] && \
   [ -n "$SUPABASE_PUBLISHABLE_KEY" ] && \
   [ -n "$SERVICE_ROLE_KEY_ASYMMETRIC" ] && \
   [ -n "$ANON_KEY_ASYMMETRIC" ]; then
  echo "Envoy sb_ key translation enabled"
else
  echo "Envoy running in legacy API key mode (sb_ keys disabled)"
fi

echo "Envoy configuration generated successfully"
echo "Starting Envoy..."

unset ENVOY_BOOTSTRAP ENVOY_CLUSTERS ENVOY_LISTENERS
exec envoy -c /etc/envoy/envoy.yaml "$@"
