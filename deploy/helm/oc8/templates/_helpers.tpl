{{/*
Expand the name of the chart.
*/}}
{{- define "oc8.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "oc8.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "oc8.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "oc8.labels" -}}
helm.sh/chart: {{ include "oc8.chart" . }}
{{ include "oc8.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "oc8.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oc8.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "oc8.componentLabels" -}}
{{ include "oc8.selectorLabels" . }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "oc8.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "oc8.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "oc8.backendImage" -}}
{{- printf "%s:%s" .Values.images.backend.repository .Values.images.backend.tag }}
{{- end }}

{{- define "oc8.frontendImage" -}}
{{- printf "%s:%s" .Values.images.frontend.repository .Values.images.frontend.tag }}
{{- end }}

{{- define "oc8.postgresHost" -}}
{{- printf "%s-postgres" (include "oc8.fullname" .) }}
{{- end }}

{{- define "oc8.redisHost" -}}
{{- printf "%s-redis" (include "oc8.fullname" .) }}
{{- end }}

{{- define "oc8.backendHost" -}}
{{- printf "%s-backend" (include "oc8.fullname" .) }}
{{- end }}

{{- define "oc8.frontendHost" -}}
{{- printf "%s-frontend" (include "oc8.fullname" .) }}
{{- end }}

{{- define "oc8.internalBaseUrl" -}}
{{- if .Values.oc8.internalBaseUrl }}
{{- .Values.oc8.internalBaseUrl }}
{{- else }}
{{- printf "http://%s:8099" (include "oc8.backendHost" .) }}
{{- end }}
{{- end }}

{{- define "oc8.ollamaBaseUrl" -}}
{{- if .Values.oc8.ollamaBaseUrl }}
{{- .Values.oc8.ollamaBaseUrl }}
{{- else if .Values.ollama.enabled }}
{{- printf "http://%s-ollama:11434" (include "oc8.fullname" .) }}
{{- else }}
{{- "" }}
{{- end }}
{{- end }}

{{- define "oc8.backendEnv" -}}
- name: OC8_ENV
  value: {{ .Values.oc8.env | quote }}
- name: OC8_DATABASE_URL
  value: {{ printf "postgresql+asyncpg://oc8_app:oc8@%s:5432/oc8" (include "oc8.postgresHost" .) | quote }}
- name: OC8_MIGRATION_URL
  value: {{ printf "postgresql+psycopg://oc8_migrate:oc8@%s:5432/oc8" (include "oc8.postgresHost" .) | quote }}
- name: OC8_REDIS_URL
  value: {{ printf "redis://%s:6379/0" (include "oc8.redisHost" .) | quote }}
- name: OC8_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "oc8.fullname" . }}-secrets
      key: jwt-secret
- name: OC8_SECRET_KEK
  valueFrom:
    secretKeyRef:
      name: {{ include "oc8.fullname" . }}-secrets
      key: secret-kek
- name: OC8_CAPAS_PATH
  value: {{ .Values.oc8.capasPath | quote }}
- name: PYTHONPATH
  value: {{ .Values.oc8.pythonPath | quote }}
- name: OC8_AGENT_ISOLATION
  value: {{ .Values.oc8.agentIsolation | quote }}
- name: OC8_INTERNAL_BASE_URL
  value: {{ include "oc8.internalBaseUrl" . | quote }}
- name: OC8_AGENT_RUNTIME_NETWORK
  value: "oc8_agents"
- name: OC8_SANDBOX_USER
  value: {{ .Values.oc8.sandboxUser | quote }}
- name: OC8_RUNTIME_SESSION_ROOT
  value: {{ .Values.backend.sessions.mountPath | quote }}
- name: OC8_EVIDENCE_SWEEP_ENABLED
  value: {{ .Values.oc8.evidence.sweepEnabled | quote }}
- name: OC8_EVIDENCE_ARCHIVE_AFTER_MINUTES
  value: {{ .Values.oc8.evidence.archiveAfterMinutes | quote }}
- name: OC8_EVIDENCE_RETENTION_DAYS
  value: {{ .Values.oc8.evidence.retentionDays | quote }}
- name: OTEL_SDK_DISABLED
  value: {{ .Values.oc8.otelSdkDisabled | quote }}
{{- if .Values.oc8.anthropicApiKey }}
- name: OC8_ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "oc8.fullname" . }}-secrets
      key: anthropic-api-key
{{- end }}
{{- if .Values.oc8.openaiApiKey }}
- name: OC8_OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "oc8.fullname" . }}-secrets
      key: openai-api-key
{{- end }}
{{- if .Values.oc8.mistralApiKey }}
- name: OC8_MISTRAL_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "oc8.fullname" . }}-secrets
      key: mistral-api-key
{{- end }}
{{- $ollamaUrl := include "oc8.ollamaBaseUrl" . }}
{{- if $ollamaUrl }}
- name: OC8_OLLAMA_BASE_URL
  value: {{ $ollamaUrl | quote }}
{{- end }}
{{- if .Values.oc8.defaultEmbeddingModel }}
- name: OC8_DEFAULT_EMBEDDING_MODEL
  value: {{ .Values.oc8.defaultEmbeddingModel | quote }}
{{- end }}
{{- end }}

{{- define "oc8.waitForPostgresInit" -}}
- name: wait-for-postgres
  image: {{ .Values.images.postgres.repository }}:{{ .Values.images.postgres.tag }}
  imagePullPolicy: {{ .Values.images.postgres.pullPolicy }}
  command:
    - sh
    - -c
    - |
      until pg_isready -h {{ include "oc8.postgresHost" . }} -p 5432 -U postgres; do
        echo "waiting for postgres..."
        sleep 2
      done
{{- end }}

{{- define "oc8.migrateInit" -}}
- name: migrate
  image: {{ include "oc8.backendImage" . }}
  imagePullPolicy: {{ .Values.images.backend.pullPolicy }}
  env:
    {{- include "oc8.backendEnv" . | nindent 4 }}
    - name: OC8_SEED_ON_START
      value: {{ .Values.oc8.seedOnStart | quote }}
  command:
    - sh
    - -c
    - |
      set -e
      alembic upgrade head
      if [ "$OC8_SEED_ON_START" = "true" ]; then
        echo "seeding..."
        oc8 seed || echo "seed skipped/failed (non-fatal)"
      fi
{{- end }}

{{- define "oc8.backendVolumes" -}}
{{- if or .Values.backend.capas.hostPath .Values.backend.capas.existingClaim }}
- name: capas
  {{- if .Values.backend.capas.existingClaim }}
  persistentVolumeClaim:
    claimName: {{ .Values.backend.capas.existingClaim }}
  {{- else }}
  hostPath:
    path: {{ .Values.backend.capas.hostPath }}
    type: Directory
  {{- end }}
{{- end }}
{{- if .Values.backend.sessions.enabled }}
- name: sessions
  persistentVolumeClaim:
    claimName: {{ include "oc8.fullname" . }}-sessions
{{- end }}
{{- end }}

{{- define "oc8.backendVolumeMounts" -}}
{{- if or .Values.backend.capas.hostPath .Values.backend.capas.existingClaim }}
- name: capas
  mountPath: {{ .Values.oc8.capasPath }}
  readOnly: true
{{- end }}
{{- if .Values.backend.sessions.enabled }}
- name: sessions
  mountPath: {{ .Values.backend.sessions.mountPath }}
{{- end }}
{{- if .Values.backend.containerSocket.enabled }}
- name: container-socket
  mountPath: /var/run/docker.sock
  readOnly: true
{{- end }}
{{- end }}
