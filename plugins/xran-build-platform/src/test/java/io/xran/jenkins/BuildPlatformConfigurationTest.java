package io.xran.jenkins;

import static org.junit.Assert.*;

import hudson.model.User;
import hudson.security.ACL;
import java.nio.file.Files;
import java.util.Map;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.csanchez.jenkins.plugins.kubernetes.KubernetesCloud;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.MockAuthorizationStrategy;
import org.junit.Rule;
import org.junit.Test;
import org.springframework.security.access.AccessDeniedException;

public class BuildPlatformConfigurationTest {
    @Rule public JenkinsRule j = new JenkinsRule();

    private BuildPlatformConfiguration seed() throws Exception {
        j.jenkins.clouds.add(new KubernetesCloud("test"));
        BuildPlatformConfiguration c = BuildPlatformConfiguration.get();
        c.initialize(Map.of("concurrency", 6, "xcn", Map.of("offline", true)));
        assertTrue(c.getStatus(), c.getStatus().contains("已生效"));
        return c;
    }

    @Test public void migrationSaveStaleEditorAndRestart() throws Exception {
        BuildPlatformConfiguration c = seed();
        assertEquals(1, c.getRevision());
        assertEquals(4, c.getForm().getInt("imageDindMemoryGiB"));
        JSONObject edit = c.getForm();
        edit.put("revision", 1);
        edit.put("concurrency", 2);
        edit.put("serviceCpu", 6);
        c.configure((org.kohsuke.stapler.StaplerRequest2) null, edit);
        assertEquals(2, c.getRevision());
        assertEquals("2", ((KubernetesCloud) j.jenkins.clouds.get(0)).getContainerCapStr());
        try { c.configure((org.kohsuke.stapler.StaplerRequest2) null, edit); fail("stale editor accepted"); }
        catch (hudson.model.Descriptor.FormException expected) { assertTrue(expected.getMessage().contains("刷新")); }
        c.initialize(Map.of("concurrency", 20));
        assertEquals(2, c.getForm().getInt("concurrency"));
        BuildPlatformConfiguration reloaded = new BuildPlatformConfiguration();
        assertEquals(c.getForm(), reloaded.getForm());
        assertEquals(2, reloaded.getRevision());
        String stored = Files.readString(j.jenkins.getRootDir().toPath().resolve("xran-platform/state.json"));
        assertTrue(stored.contains("web-save"));
        assertTrue(stored.contains("site-migration"));
        assertTrue(stored.contains("before"));
        assertTrue(stored.contains("after"));
    }

    @Test public void budgetsAreValidatedBeforeSave() throws Exception {
        BuildPlatformConfiguration c = seed();
        for (String key : new String[] {"concurrency", "serviceCpu", "xcnMemoryGiB", "innerBuildMemoryGiB", "agentStartupTimeoutMinutes"}) {
            JSONObject edit = c.getForm();
            edit.put("revision", c.getRevision());
            edit.put(key, key.equals("innerBuildMemoryGiB") ? 4 : 0);
            try { c.configure((org.kohsuke.stapler.StaplerRequest2) null, edit); fail("invalid value accepted: " + key); }
            catch (hudson.model.Descriptor.FormException expected) { assertEquals(1, c.getRevision()); }
        }
        JSONObject fraction = c.getForm();
        fraction.put("serviceCpu", 2.5);
        assertThrows(IllegalArgumentException.class, () -> PlatformSettings.validate(fraction));
        for (int temporary : new int[] {1, 2, 6, 20}) {
            JSONObject s = c.getForm();
            for (String p : new String[] {"service", "image", "xcn"}) s.put(p + "TemporaryStorageGiB", temporary);
            PlatformSettings.validate(s);
        }
    }

    @Test public void serialSettingMigratesOnceAndKeepsAdministratorValues() throws Exception {
        BuildPlatformConfiguration c = seed();
        var file = j.jenkins.getRootDir().toPath().resolve("xran-platform/state.json");
        JSONObject stored = JSONObject.fromObject(Files.readString(file));
        stored.getJSONObject("settings").remove("xranServiceSerial");
        stored.getJSONObject("settings").put("serviceMemoryGiB", 16);
        stored.getJSONObject("settings").put("concurrency", 3);
        String leases = stored.getJSONObject("leases").toString();
        Files.writeString(file, stored.toString());
        BuildPlatformConfiguration migrated = new BuildPlatformConfiguration();
        assertTrue(migrated.getForm().getBoolean("xranServiceSerial"));
        assertEquals(16, migrated.getForm().getInt("serviceMemoryGiB"));
        assertEquals(3, migrated.getForm().getInt("concurrency"));
        assertEquals(2, migrated.getRevision());
        JSONObject saved = JSONObject.fromObject(Files.readString(file));
        assertEquals(leases, saved.getJSONObject("leases").toString());
        assertTrue(Files.readString(file).contains("build-compatibility-upgrade"));
        assertEquals(2, new BuildPlatformConfiguration().getRevision());
        JSONObject edit = migrated.getForm();
        edit.put("revision", migrated.getRevision());
        edit.put("xranServiceSerial", false);
        migrated.configure((org.kohsuke.stapler.StaplerRequest2) null, edit);
        assertFalse(new BuildPlatformConfiguration().getForm().getBoolean("xranServiceSerial"));
        assertEquals(false, migrated.effective("service").get("xranServiceSerial"));
    }

    @Test public void savedProjectSettingsKeepValuesAcrossRename() {
        JSONObject expected = PlatformSettings.defaults();
        expected.put("concurrency", 3);
        expected.put("xcnCpu", 7);
        expected.put("xcnMemoryGiB", 12);
        expected.put("xcnOffline", false);
        expected.put("compilerImage", "registry.local/open5gs/sdk:stable");
        JSONObject previous = JSONObject.fromObject(expected.toString());
        for (Object name : expected.keySet()) {
            String key = name.toString();
            if (key.startsWith("xcn")) previous.put("open5gs" + key.substring(3), previous.remove(key));
        }
        JSONObject upgraded = PlatformSettings.upgradeNames(previous);
        assertEquals(expected, PlatformSettings.validate(upgraded));
        assertEquals(upgraded, PlatformSettings.upgradeNames(upgraded));
        assertFalse(previous.containsKey("xcnCpu"));
        previous.put("xcnCpu", 8);
        assertThrows(IllegalArgumentException.class, () -> PlatformSettings.upgradeNames(previous));
        JSONObject oldSite = new JSONObject();
        oldSite.put("open5gs", Map.of("offline", false));
        assertFalse(PlatformSettings.migrate(oldSite).getBoolean("xcnOffline"));
    }

    @Test public void trustedPipelineParsesWithInstalledPlugin() throws Exception {
        seed();
        java.nio.file.Path root = java.nio.file.Path.of("../..");
        String source = Files.readString(root.resolve("jenkins/storage.groovy")) + "\n"
                + Files.readString(root.resolve("jenkins/compiler.groovy")) + "\n"
                + Files.readString(root.resolve("jenkins/pipeline.groovy"));
        try (groovy.lang.GroovyClassLoader loader = new groovy.lang.GroovyClassLoader(j.jenkins.getPluginManager().uberClassLoader)) {
            assertNotNull(loader.parseClass(source));
        }
    }

    @Test public void applyFailureBlocksAdmissionAndRecovers() throws Exception {
        BuildPlatformConfiguration c = seed();
        var cloud = j.jenkins.clouds.get(0);
        j.jenkins.clouds.remove(cloud);
        c.doReapply();
        assertTrue(c.getStatus().contains("应用失败"));
        assertThrows(IllegalStateException.class, () -> c.tryAdmit(null, "service"));
        j.jenkins.clouds.add(cloud);
        c.doReapply();
        assertTrue(c.getStatus().contains("已生效"));
        assertEquals(1, c.getRevision());
    }

    @Test public void nativeFormAndAllActionsAreAdminOnly() throws Exception {
        BuildPlatformConfiguration c = seed();
        JSONObject expected = c.getForm();
        expected.put("minimumFreePercent", 0);
        expected.put("xcnOffline", false);
        expected.put("compilerImagePullPolicy", "Never");
        expected.put("xranServiceSerial", false);
        JSONObject submitted = JSONObject.fromObject(expected.toString());
        submitted.put("revision", c.getRevision());
        c.configure((org.kohsuke.stapler.StaplerRequest2) null, submitted);
        j.jenkins.setSecurityRealm(j.createDummySecurityRealm());
        j.jenkins.setAuthorizationStrategy(new MockAuthorizationStrategy()
                .grant(Jenkins.ADMINISTER).everywhere().to("admin")
                .grant(Jenkins.READ).everywhere().to("builder"));
        var admin = j.createWebClient().login("admin");
        var page = admin.goTo("configure");
        assertTrue(page.asNormalizedText().contains("构建平台设置"));
        assertTrue(page.asNormalizedText().contains("xcn 离线构建"));
        assertTrue(page.asNormalizedText().contains("X-RAN-CLOUD 服务编译串行执行"));
        assertFalse(page.asNormalizedText().contains("open5gs"));
        try { j.submit(page.getFormByName("config")); }
        catch (org.htmlunit.FailingHttpStatusCodeException error) {
            System.err.println(error.getResponse().getContentAsString());
            throw error;
        }
        assertEquals(expected, c.getForm());
        try (var ignored = ACL.as2(User.getById("builder", true).impersonate2())) {
            assertThrows(AccessDeniedException.class, c::getForm);
            assertThrows(AccessDeniedException.class, c::getRevision);
            assertThrows(AccessDeniedException.class, c::getStatus);
            assertThrows(AccessDeniedException.class, c::getAuditText);
            assertThrows(AccessDeniedException.class, c::doState);
            assertThrows(AccessDeniedException.class, c::doReapply);
            assertThrows(AccessDeniedException.class, c::doFillCompilerImagePullPolicyItems);
            assertThrows(AccessDeniedException.class, () -> c.doPreview(null));
            assertThrows(AccessDeniedException.class, () -> c.configure((org.kohsuke.stapler.StaplerRequest2) null, new JSONObject()));
        }
        try (var ignored = ACL.as2(Jenkins.ANONYMOUS2)) {
            assertThrows(AccessDeniedException.class, c::getForm);
            assertThrows(AccessDeniedException.class, c::doState);
        }
    }
}
