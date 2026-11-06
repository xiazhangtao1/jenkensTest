package io.xran.jenkins;

import hudson.Extension;
import hudson.model.Run;
import hudson.model.listeners.RunListener;
import hudson.security.ACL;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;

@Extension
public final class LeaseCleanup extends RunListener<Run<?, ?>> {
    private static final Logger LOG = Logger.getLogger(LeaseCleanup.class.getName());

    @Override public void onFinalized(Run<?, ?> run) {
        try (var ignored = ACL.as2(ACL.SYSTEM2)) {
            BuildPlatformConfiguration.get().release(run.getExternalizableId());
        } catch (IOException e) { LOG.log(Level.WARNING, "Cannot release build admission", e); }
    }
}
