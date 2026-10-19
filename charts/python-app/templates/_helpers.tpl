{{- define "python-app.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-"}}
{{- end }}

{{- define "python-app.selectorLabels" -}}
app: {{ include "python-app.name" . }}
{{- end }}

{{- define "python-app.labels" -}}
{{ include "python-app.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{.Chart.Version }}
{{- end }}