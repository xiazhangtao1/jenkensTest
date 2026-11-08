package io.xran.jenkins;

import static org.junit.Assert.*;

import hudson.model.Item;
import hudson.model.User;
import hudson.security.ACL;
import hudson.security.AuthorizationMatrixProperty;
import hudson.security.ProjectMatrixAuthorizationStrategy;
import java.nio.file.Files;
import java.util.Map;
import java.util.Set;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.csanchez.jenkins.plugins.kubernetes.KubernetesCloud;
import org.jenkinsci.plugins.matrixauth.PermissionEntry;
import org.jenkinsci.plugins.matrixauth.inheritance.NonInheritingStrategy;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.junit.Rule;
import org.junit.Test;
import org.jvnet.hudson.test.JenkinsRule;

public class JobVisibilityTest {
    @Rule public JenkinsRule j = new JenkinsRule();

    private BuildPlatformConfiguration seed() throws Exception {
        j.jenkins.setSecurityRealm(j.createDummySecurityRealm());
        ProjectMatrixAuthorizationStrategy strategy = new ProjectMatrixAuthorizationStrategy();
        strategy.add(Jenkins.ADMINISTER, PermissionEntry.user("admin"));
        strategy.add(Jenkins.READ, PermissionEntry.group("authenticated"));
        strategy.add(Item.READ, PermissionEntry.group("authenticated"));
        strategy.add(Item.BUILD, PermissionEntry.group("authenticated"));
        j.jenkins.setAuthorizationStrategy(strategy);
        j.jenkins.clouds.add(new KubernetesCloud("test"));
        for (String name : new String[]{"x-ran-cloud", "xcn", "storage-maintenance"}) j.jenkins.createProject(WorkflowJob.class, name);
        BuildPlatformConfiguration c = BuildPlatformConfiguration.get();
        c.initialize(Map.of());
        return c;
    }

    private void save(BuildPlatformConfiguration c, boolean xran, boolean xcn) throws Exception {
        JSONObject edit = c.getForm();
        edit.put("revision", c.getRevision());
        edit.put("xranAdminOnly", xran); edit.put("xcnAdminOnly", xcn);
        c.configure((org.kohsuke.stapler.StaplerRequest2)null, edit);
        assertTrue(c.getStatus(), c.getStatus().contains("已生效"));
    }

    private boolean readable(WorkflowJob job, String user) throws Exception {
        try (var ignored = ACL.as2(User.getById(user, true).impersonate2())) { return job.hasPermission(Item.READ); }
    }

    @Test public void fourCombinationsAndRestartPersistence() throws Exception {
        BuildPlatformConfiguration c = seed();
        WorkflowJob xran = (WorkflowJob)j.jenkins.getItem("x-ran-cloud"), xcn = (WorkflowJob)j.jenkins.getItem("xcn");
        WorkflowJob maintenance = (WorkflowJob)j.jenkins.getItem("storage-maintenance");
        for (boolean a : new boolean[]{false, true}) for (boolean b : new boolean[]{false, true}) {
            save(c, a, b);
            assertEquals(!a, readable(xran, "builder")); assertEquals(!b, readable(xcn, "builder"));
            assertFalse(readable(maintenance, "builder"));
            assertTrue(readable(xran, "admin")); assertTrue(readable(xcn, "admin"));
        }
        BuildPlatformConfiguration loaded = new BuildPlatformConfiguration();
        assertTrue(loaded.getForm().getBoolean("xranAdminOnly"));
        assertTrue(loaded.getForm().getBoolean("xcnAdminOnly"));
        assertTrue(Files.readString(j.jenkins.getRootDir().toPath().resolve("xran-platform/state.json")).contains("authorizationBackups"));
        save(c, false, false);
        assertNull(xran.getProperty(AuthorizationMatrixProperty.class));
        assertNull(xcn.getProperty(AuthorizationMatrixProperty.class));
    }

    @Test public void customUserGroupAndInheritanceAreRestoredAndDriftIsRepaired() throws Exception {
        BuildPlatformConfiguration c = seed();
        WorkflowJob job = (WorkflowJob)j.jenkins.getItem("x-ran-cloud");
        job.addProperty(new AuthorizationMatrixProperty(Map.of(Item.READ,
                Set.of(PermissionEntry.user("chosen"), PermissionEntry.group("developers"))), new NonInheritingStrategy()));
        String original = Jenkins.XSTREAM2.toXML(job.getProperty(AuthorizationMatrixProperty.class));
        for (int i=0; i<2; i++) {
            save(c, true, false);
            assertFalse(readable(job, "chosen"));
            job.removeProperty(AuthorizationMatrixProperty.class);
            c.maintain();
            assertFalse(readable(job, "builder"));
            save(c, false, false);
            assertEquals(original, Jenkins.XSTREAM2.toXML(job.getProperty(AuthorizationMatrixProperty.class)));
            assertTrue(readable(job, "chosen")); assertFalse(readable(job, "builder"));
        }
    }

    @Test public void visibilityAppliesEvenWithoutKubernetesCloud() throws Exception {
        BuildPlatformConfiguration c = seed();
        j.jenkins.clouds.clear();
        JSONObject edit = c.getForm();
        edit.put("revision",c.getRevision()); edit.put("xranAdminOnly",true);
        c.configure((org.kohsuke.stapler.StaplerRequest2)null,edit);
        assertTrue(c.getStatus().contains("应用失败"));
        assertFalse(readable((WorkflowJob)j.jenkins.getItem("x-ran-cloud"),"builder"));
        assertTrue(readable((WorkflowJob)j.jenkins.getItem("x-ran-cloud"),"admin"));
    }

    @Test public void upgradeDoesNotOpenExistingPrivateJobs() throws Exception {
        BuildPlatformConfiguration c = seed();
        WorkflowJob job = (WorkflowJob)j.jenkins.getItem("x-ran-cloud");
        job.addProperty(new AuthorizationMatrixProperty(Map.of(),new NonInheritingStrategy()));
        var file = j.jenkins.getRootDir().toPath().resolve("xran-platform/state.json");
        JSONObject state = JSONObject.fromObject(Files.readString(file));
        state.getJSONObject("settings").remove("xranAdminOnly"); state.getJSONObject("settings").remove("xcnAdminOnly");
        Files.writeString(file,state.toString());
        BuildPlatformConfiguration loaded = new BuildPlatformConfiguration();
        assertFalse(loaded.getForm().getBoolean("xranAdminOnly"));
        assertFalse(readable(job, "builder"));
        assertEquals(2,loaded.getRevision());
        assertEquals(2,new BuildPlatformConfiguration().getRevision());
    }
}
