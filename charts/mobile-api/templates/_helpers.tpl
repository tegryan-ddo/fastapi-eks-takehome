{{/*
Resource name: "<release>-mobile-api", or just the release name when it
already contains the chart name. Truncated to the 63-character DNS limit.
*/}}
{{- define "mobile-api.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Labels shared by every selector in this release. Deployment selectors are
immutable, so nothing that changes between releases (version, chart version)
belongs here. Each workload adds its own app.kubernetes.io/component.
*/}}
{{- define "mobile-api.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Metadata labels for every resource. */}}
{{- define "mobile-api.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "mobile-api.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Image reference: repository@digest when a digest is set, else repository:tag. */}}
{{- define "mobile-api.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end -}}
