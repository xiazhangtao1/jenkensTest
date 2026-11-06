package io.xran.jenkins;

import hudson.Extension;
import hudson.model.ChoiceParameterDefinition;
import hudson.model.ParameterDefinition;
import hudson.model.ParametersDefinitionProperty;
import hudson.model.Run;
import hudson.model.StringParameterDefinition;
import hudson.util.AtomicFileWriter;
import hudson.util.FormValidation;
import hudson.util.ListBoxModel;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import jenkins.model.GlobalConfiguration;
import jenkins.model.Jenkins;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;
import org.csanchez.jenkins.plugins.kubernetes.KubernetesCloud;
import org.jenkins.plugins.lockableresources.LockableResourcesManager;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.kohsuke.stapler.StaplerRequest2;
import org.kohsuke.stapler.interceptor.RequirePOST;

/** Native administrator-only configuration; the single atomic state includes leases and audit. */
@Extension
public final class BuildPlatformConfiguration extends GlobalConfiguration {
    private transient JSONObject state;

    public BuildPlatformConfiguration() {
        File file = stateFile();
        try {
            state = file.isFile() ? JSONObject.fromObject(Files.readString(file.toPath())) : new JSONObject();
        } catch (Exception e) {
            // Do not silently replace damaged persisted settings with site defaults.
            throw new IllegalStateException("无法读取构建平台持久化配置：" + file, e);
        }
    }

    public static BuildPlatformConfiguration get() {
        return GlobalConfiguration.all().get(BuildPlatformConfiguration.class);
    }

    @Override public String getDisplayName() { return "构建平台设置"; }
    @Override public hudson.security.Permission getRequiredGlobalConfigPagePermission() { return Jenkins.ADMINISTER; }
    public boolean isAdmin() { return Jenkins.get().hasPermission(Jenkins.ADMINISTER); }
    private static void admin() { Jenkins.get().checkPermission(Jenkins.ADMINISTER); }
    private static File stateFile() { return new File(Jenkins.get().getRootDir(), "xran-platform/state.json"); }

    private void persist() throws IOException {
        File file = stateFile();
        Files.createDirectories(Objects.requireNonNull(file.toPath().getParent()));
        try (AtomicFileWriter writer = new AtomicFileWriter(file.toPath(), StandardCharsets.UTF_8)) {
            try {
                writer.write(state.toString(2) + "\n");
                writer.commit();
            } finally { writer.abort(); }
        }
    }

    /** Called after JCasC and the two trusted jobs are initialized. Site seeds once only. */
    public synchronized void initialize(Map<String, Object> site) throws IOException {
        admin();
        if (!state.containsKey("settings")) {
            state.put("settings", PlatformSettings.migrate(JSONObject.fromObject(site)));
            state.put("version", 1L);
            state.put("audit", new JSONArray());
            state.put("leases", new JSONObject());
            audit(null, state.getJSONObject("settings"), "site-migration");
            persist();
        }
        prune();
        apply();
    }

    public synchronized JSONObject getForm() { admin(); return copy(settings()); }
    public synchronized long getRevision() { admin(); return state.optLong("version", 0L); }
    public synchronized String getStatus() {
        admin();
        return "配置版本 " + state.optLong("version") + "；已应用版本 " + state.optLong("appliedVersion")
                + "；运行中 " + leases().size() + "；" + state.optString("applyError", "已生效");
    }
    public synchronized String getAuditText() {
        admin();
        JSONArray all = state.optJSONArray("audit");
        if (all == null) return "";
        JSONArray recent = new JSONArray();
        for (int i = Math.max(0, all.size() - 20); i < all.size(); i++) recent.add(all.get(i));
        return recent.toString(2);
    }
    public synchronized Map<String, Object> effective(String profile) {
        admin();
        return PlatformSettings.runtime(settings(), profile);
    }

    private JSONObject settings() {
        if (!state.containsKey("settings")) throw new IllegalStateException("构建平台尚未初始化");
        return state.getJSONObject("settings");
    }
    private JSONObject leases() {
        if (!state.containsKey("leases")) state.put("leases", new JSONObject());
        return state.getJSONObject("leases");
    }
    private static JSONObject copy(JSONObject value) { return JSONObject.fromObject(value.toString()); }

    @Override public synchronized boolean configure(StaplerRequest2 req, JSONObject json) throws FormException {
        admin();
        try {
            long expected = json.getLong("revision");
            if (expected != state.getLong("version")) throw new IllegalArgumentException("设置已被其他管理员更新，请刷新页面后重试");
            JSONObject candidate = PlatformSettings.validate(json);
            JSONObject before = copy(settings());
            if (!before.equals(candidate)) {
                state.put("settings", candidate);
                state.put("version", state.getLong("version") + 1);
                audit(before, candidate, "web-save");
                // Save desired configuration before side effects. A failed apply blocks admission.
                state.put("applyError", "等待应用配置");
                persist();
            }
            apply();
            return true;
        } catch (IllegalArgumentException | IOException e) {
            throw new FormException(e.getMessage(), "xran-build-platform");
        }
    }

    private void audit(JSONObject before, JSONObject after, String reason) {
        JSONArray events = state.optJSONArray("audit");
        if (events == null) { events = new JSONArray(); state.put("audit", events); }
        JSONObject event = new JSONObject();
        event.put("at", OffsetDateTime.now().toString());
        event.put("user", Jenkins.getAuthentication2().getName());
        event.put("version", state.optLong("version"));
        event.put("reason", reason);
        event.put("before", before);
        event.put("after", copy(after));
        events.add(event);
    }

    /** All externally reachable actions enforce Administer and CSRF-protected POST. */
    @RequirePOST public synchronized FormValidation doPreview(StaplerRequest2 req) {
        admin();
        try {
            JSONObject candidate = copy(settings());
            for (Object name : candidate.keySet().toArray()) {
                String key = name.toString(), value = req.getParameter(key);
                if (value != null) candidate.put(key, key.equals("open5gsOffline") ? Boolean.valueOf(value) : value);
            }
            candidate = PlatformSettings.validate(candidate);
            JSONObject result = new JSONObject();
            for (String p : List.of("service", "image", "open5gs")) result.put(p, PlatformSettings.allocation(candidate, p));
            return FormValidation.ok(result.toString(2));
        } catch (RuntimeException e) { return FormValidation.error(e.getMessage()); }
    }

    @RequirePOST public synchronized FormValidation doReapply() throws IOException {
        admin();
        apply();
        return state.containsKey("applyError") ? FormValidation.error(state.getString("applyError")) : FormValidation.ok("构建平台配置已生效");
    }

    @RequirePOST public synchronized FormValidation doState() {
        admin();
        return FormValidation.ok(state.toString(2));
    }

    public synchronized ListBoxModel doFillCompilerImagePullPolicyItems() {
        admin();
        ListBoxModel choices = new ListBoxModel();
        for (String policy : List.of("Always", "IfNotPresent", "Never")) {
            choices.add(new ListBoxModel.Option(policy, policy, policy.equals(settings().getString("compilerImagePullPolicy"))));
        }
        return choices;
    }

    private void apply() throws IOException {
        try {
            int cap = settings().getInt("concurrency");
            boolean found = false;
            for (var cloud : Jenkins.get().clouds) {
                if (cloud instanceof KubernetesCloud k) { k.setContainerCapStr(Integer.toString(cap)); found = true; }
            }
            if (!found) throw new IllegalStateException("未找到 Kubernetes Cloud，暂停接收新构建");
            var locks = LockableResourcesManager.get();
            synchronized (LockableResourcesManager.syncResources) {
            for (int i = 1; i <= cap; i++) {
                String name = "xran-slot-" + i;
                locks.createResourceWithLabel(name, "xran-build-slot");
                locks.getResources().stream().filter(r -> r.getName().equals(name)).findFirst().orElseThrow().setLabels("xran-build-slot");
            }
            for (var resource : locks.getResources()) {
                if (resource.getName().matches("xran-slot-[0-9]+")
                        && Integer.parseInt(resource.getName().substring(10)) > cap) resource.setLabels("");
            }
            locks.save();
            locks.refreshQueue();
            }
            updateDefaults();
            Jenkins.get().save();
            state.put("appliedVersion", state.getLong("version"));
            state.remove("applyError");
        } catch (IOException | RuntimeException e) {
            state.put("applyError", "应用失败：" + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        persist();
    }

    private void updateDefaults() throws IOException {
        for (String name : List.of("x-ran-cloud", "open5gs")) {
            WorkflowJob job = Jenkins.get().getItemByFullName(name, WorkflowJob.class);
            if (job == null) continue;
            ParametersDefinitionProperty previous = job.getProperty(ParametersDefinitionProperty.class);
            if (previous == null) continue;
            List<ParameterDefinition> defs = new ArrayList<>();
            for (ParameterDefinition p : previous.getParameterDefinitions()) {
                if (p.getName().equals("COMPILER_IMAGE")) {
                    p = new StringParameterDefinition(p.getName(), settings().getString("compilerImage"), "编译环境");
                } else if (p.getName().equals("COMPILER_IMAGE_PULL_POLICY")) {
                    List<String> choices = new ArrayList<>(List.of("Always", "IfNotPresent", "Never"));
                    choices.remove(settings().getString("compilerImagePullPolicy"));
                    choices.add(0, settings().getString("compilerImagePullPolicy"));
                    p = new ChoiceParameterDefinition(p.getName(), choices.toArray(String[]::new), "拉取策略");
                }
                defs.add(p);
            }
            job.removeProperty(ParametersDefinitionProperty.class);
            job.addProperty(new ParametersDefinitionProperty(defs));
            job.save();
        }
    }

    /** Atomic persistent admission. Returns only plain serializable Maps for trusted CPS callers. */
    public synchronized Map<String, Object> tryAdmit(Run<?, ?> run, String profile) throws IOException {
        admin();
        if (state.containsKey("applyError") || state.optLong("appliedVersion") != state.optLong("version")) {
            throw new IllegalStateException(state.optString("applyError", "构建平台配置尚未应用"));
        }
        String id = run.getExternalizableId();
        prune();
        if (leases().containsKey(id)) {
            JSONObject lease = leases().getJSONObject(id);
            if (!Objects.equals(lease.getString("resourceProfile"), profile)) throw new IllegalStateException("构建的资源配置类型发生变化");
            return plain(lease);
        }
        if (!run.isBuilding()) throw new IllegalArgumentException("只能为运行中的构建申请槽位");
        if (leases().size() >= settings().getInt("concurrency")) return null;
        JSONObject lease = new JSONObject();
        lease.put("settingsVersion", state.getLong("version"));
        lease.put("resourceProfile", profile);
        lease.put("resourceAllocation", PlatformSettings.allocation(settings(), profile));
        lease.put("config", PlatformSettings.runtime(settings(), profile));
        lease.put("admittedAt", OffsetDateTime.now().toString());
        lease.put("admittedAtMillis", System.currentTimeMillis());
        leases().put(id, lease);
        persist();
        return plain(lease);
    }

    public synchronized void release(String id) throws IOException {
        admin();
        if (leases().remove(id) != null) persist();
    }

    /** Run records are authoritative across abort, failure and controller restart. */
    public synchronized void prune() throws IOException {
        admin();
        boolean changed = false;
        for (Object id : leases().keySet().toArray()) {
            Run<?, ?> run = Run.fromExternalizableId(id.toString());
            if (run == null || !run.isBuilding()) { leases().remove(id); changed = true; }
        }
        if (changed) persist();
    }

    /** Reconcile drift from JCasC or other sections of the native System form. */
    synchronized void maintain() throws IOException {
        admin();
        if (!state.containsKey("settings")) return;
        prune();
        int cap = settings().getInt("concurrency");
        boolean cloudFound = false, drift = state.containsKey("applyError");
        for (var cloud : Jenkins.get().clouds) {
            if (cloud instanceof KubernetesCloud k) {
                cloudFound = true;
                drift |= !Integer.toString(cap).equals(k.getContainerCapStr());
            }
        }
        var manager = LockableResourcesManager.get();
        synchronized (LockableResourcesManager.syncResources) {
            for (int i = 1; i <= cap; i++) {
                String name = "xran-slot-" + i;
                var slot = manager.getResources().stream().filter(r -> r.getName().equals(name)).findFirst().orElse(null);
                drift |= slot == null || !slot.getLabelsAsList().contains("xran-build-slot");
            }
            for (var slot : manager.getResources()) {
                if (slot.getName().matches("xran-slot-[0-9]+") && Integer.parseInt(slot.getName().substring(10)) > cap) {
                    drift |= slot.getLabelsAsList().contains("xran-build-slot");
                }
            }
        }
        if (!cloudFound || drift) apply();
    }

    private static Map<String, Object> plain(JSONObject json) {
        Map<String, Object> result = new LinkedHashMap<>();
        for (Object item : json.entrySet()) {
            Map.Entry<?, ?> entry = (Map.Entry<?, ?>) item;
            Object value = entry.getValue();
            if (value instanceof JSONObject object) value = plain(object);
            result.put(entry.getKey().toString(), value);
        }
        return result;
    }
}
