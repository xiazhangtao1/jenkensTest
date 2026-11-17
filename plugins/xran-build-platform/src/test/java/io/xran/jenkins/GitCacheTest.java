package io.xran.jenkins;

import static org.junit.Assert.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class GitCacheTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();
    private static final String KEY = "a".repeat(64);
    private Path entry(Path root, String project, long used) throws Exception {
        Path folder = root.resolve(project).resolve(KEY + ".git");
        Files.createDirectories(folder);
        Files.writeString(folder.resolve("payload"), "cached-data");
        Files.writeString(folder.getParent().resolve(KEY + ".json"), "{\"lastUsedAt\":" + used + ",\"bytes\":11}");
        return folder;
    }
    @Test public void budgetIsSharedLruAndClearKeepsLockInode() throws Exception {
        Path root = temporary.newFolder().toPath();
        GitCache cache = new GitCache(root);
        cache.initialize(List.of("x-ran-cloud", "xcn", "xtue-cloud"));
        Path oldest = entry(root,"x-ran-cloud",1), middle = entry(root,"xcn",2), newest = entry(root,"xtue-cloud",3);
        var result = cache.enforce(22,Map.of());
        assertEquals(1, ((List<?>)result.get("deleted")).size());
        assertFalse(Files.exists(oldest)); assertTrue(Files.exists(middle)); assertTrue(Files.exists(newest));
        Object inode = Files.getAttribute(middle.getParent().resolve(".lock"),"unix:ino");
        assertTrue(cache.clear("xcn")); assertFalse(Files.exists(middle));
        assertEquals(inode,Files.getAttribute(middle.getParent().resolve(".lock"),"unix:ino"));
    }
    @Test public void externalFlockProtectsBusyCacheAndReleaseAllowsEviction() throws Exception {
        Path root = temporary.newFolder().toPath();
        GitCache cache = new GitCache(root);cache.initialize(List.of("xcn"));
        Path folder=entry(root,"xcn",1);
        Process holder = new ProcessBuilder("/usr/bin/flock",folder.getParent().resolve(".lock").toString(),
                "/bin/sh","-c","printf 'locked\\n'; read -r release").start();
        try {
            assertEquals("locked",new java.io.BufferedReader(new java.io.InputStreamReader(holder.getInputStream())).readLine());
            assertFalse(cache.clear("xcn"));
            assertEquals(List.of("xcn"),cache.enforce(0,Map.of()).get("busy"));
            assertTrue(Files.exists(folder));
        } finally {holder.getOutputStream().close();holder.waitFor();}
        assertTrue(cache.clear("xcn")); assertFalse(Files.exists(folder));
    }
    @Test public void symlinkCannotEscapeCacheAndOtherDiskPressureDoesNotEvict() throws Exception {
        Path root=temporary.newFolder().toPath();GitCache cache=new GitCache(root);cache.initialize(List.of("xcn"));
        Path folder=entry(root,"xcn",1);
        assertEquals(List.of(),cache.enforce(100,Map.of("unrelated-device",Long.MAX_VALUE)).get("deleted"));
        Path outside=temporary.newFolder().toPath();Files.writeString(outside.resolve("keep"),"keep");
        Files.createSymbolicLink(folder.resolve("link"),outside);
        assertTrue(cache.clear("xcn"));assertTrue(Files.exists(outside.resolve("keep")));
        Files.createSymbolicLink(folder,outside);
        assertThrows(java.io.IOException.class,()->cache.clear("xcn"));
    }
    @Test public void firstFetchFailureRemainsVisibleAndCanBeCleared() throws Exception {
        Path root=temporary.newFolder().toPath();GitCache cache=new GitCache(root);cache.initialize(List.of("xcn"));
        Path state=root.resolve("xcn").resolve(KEY+".json");
        Files.writeString(state,"{\"status\":\"error\",\"lastError\":\"authentication failed\",\"bytes\":0}");
        var status=cache.status(20L*1073741824);
        var entries=(List<?>)status.get("entries");assertEquals(1,entries.size());
        assertEquals("authentication failed",((Map<?,?>)entries.get(0)).get("lastError"));
        assertEquals(0L,status.get("bytes"));assertTrue(cache.clear("xcn"));assertFalse(Files.exists(state));
    }
}
