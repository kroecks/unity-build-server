// Installed by Unity Build Server before each build. Do not commit; the build
// writes this into Assets/Editor/ fresh every run (after git clean), so it's
// always present and always current regardless of what's in the repo.
#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

public static class BuildScript
{
    public static void PerformiOSBuild()
    {
        string outPath = GetArg("-customBuildPath") ?? "xcode-out";
        var opts = new BuildPlayerOptions {
            scenes = EnabledScenes(), locationPathName = outPath,
            target = BuildTarget.iOS, options = BuildOptions.None,
        };
        var report = BuildPipeline.BuildPlayer(opts);
        if (report.summary.result != BuildResult.Succeeded)
            EditorApplication.Exit(1);
        Debug.Log($"Build succeeded: {report.summary.totalSize} bytes at {outPath}");
    }

    /// <summary>
    /// Android / Quest player build. Unity emits the .apk/.aab directly (no
    /// second toolchain). Driven by env from the build server:
    ///   UBS_ANDROID_AAB      "1" = App Bundle (.aab, Play Store), else .apk
    ///   UBS_KEYSTORE_PATH    release keystore; blank = Unity debug keystore
    ///   UBS_KEYSTORE_ALIAS, UBS_KEYSTORE_PASS, UBS_KEY_ALIAS_PASS
    ///   UBS_BUNDLE_ID        expected package name (warned on mismatch only)
    /// Passwords are set in memory only: Unity never serializes keystore/alias
    /// passwords into ProjectSettings, and the build server resets the tree
    /// every run, so nothing leaks into git.
    /// </summary>
    public static void PerformAndroidBuild()
    {
        try
        {
            bool aab = Env("UBS_ANDROID_AAB") == "1";
            string ext = aab ? ".aab" : ".apk";
            string outPath = GetArg("-customBuildPath") ?? Path.Combine("android-out", "app" + ext);
            if (!outPath.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
                outPath = Path.ChangeExtension(outPath, ext);
            var dir = Path.GetDirectoryName(Path.GetFullPath(outPath));
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

            EditorUserBuildSettings.buildAppBundle = aab;
            EditorUserBuildSettings.exportAsGoogleAndroidProject = false;  // want the binary, not a Gradle project
            ConfigureAndroidSigning(aab);

            var expected = Env("UBS_BUNDLE_ID");
            var actual = PlayerSettings.GetApplicationIdentifier(NamedBuildTarget.Android);
            if (!string.IsNullOrEmpty(expected) && expected != actual)
                Debug.LogWarning($"[BuildScript] Package name in Player Settings is '{actual}' but the "
                    + $"build server project says '{expected}'. Install works, but launch/logs "
                    + "target the server's value — make them match.");

            Debug.Log($"[BuildScript] Android build → {outPath} ({(aab ? "AAB" : "APK")}), "
                + $"version {PlayerSettings.bundleVersion} ({PlayerSettings.Android.bundleVersionCode}), "
                + $"package {actual}, backend {PlayerSettings.GetScriptingBackend(NamedBuildTarget.Android)}, "
                + $"arch {PlayerSettings.Android.targetArchitectures}");

            var opts = new BuildPlayerOptions {
                scenes = EnabledScenes(), locationPathName = outPath,
                target = BuildTarget.Android, targetGroup = BuildTargetGroup.Android,
                options = BuildOptions.None,
            };
            var report = BuildPipeline.BuildPlayer(opts);
            if (report.summary.result != BuildResult.Succeeded)
            {
                Debug.LogError($"[BuildScript] Android build {report.summary.result} "
                    + $"({report.summary.totalErrors} error(s)).");
                EditorApplication.Exit(1);
                return;
            }
            Debug.Log($"Build succeeded: {report.summary.totalSize} bytes at {outPath}");
        }
        catch (Exception e)
        {
            Debug.LogError($"[BuildScript] PerformAndroidBuild FAILED: {e}");
            EditorApplication.Exit(1);
        }
    }

    private static void ConfigureAndroidSigning(bool aab)
    {
        var ks = Env("UBS_KEYSTORE_PATH");
        if (string.IsNullOrEmpty(ks))
        {
            PlayerSettings.Android.useCustomKeystore = false;
            Debug.Log("[BuildScript] Signing with the Unity DEBUG keystore.");
            if (aab)
                Debug.LogWarning("[BuildScript] Debug-signed AAB: Google Play will reject this. "
                    + "Set a keystore on the project for Play uploads.");
            return;
        }
        if (!File.Exists(ks)) throw new Exception($"Keystore not found: {ks}");
        var alias = Env("UBS_KEYSTORE_ALIAS");
        var storePass = Env("UBS_KEYSTORE_PASS");
        var keyPass = Env("UBS_KEY_ALIAS_PASS");
        if (string.IsNullOrEmpty(alias)) throw new Exception("Keystore set but UBS_KEYSTORE_ALIAS is empty.");
        if (string.IsNullOrEmpty(storePass)) throw new Exception("Keystore set but no keystore password.");
        if (string.IsNullOrEmpty(keyPass)) keyPass = storePass;

        PlayerSettings.Android.useCustomKeystore = true;
        PlayerSettings.Android.keystoreName = ks;
        PlayerSettings.Android.keystorePass = storePass;
        PlayerSettings.Android.keyaliasName = alias;
        PlayerSettings.Android.keyaliasPass = keyPass;
        Debug.Log($"[BuildScript] Signing with keystore {Path.GetFileName(ks)}, alias '{alias}'.");
    }

    private static string[] EnabledScenes()
    {
        var scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
        if (scenes.Length == 0) throw new Exception("No enabled scenes in Build Settings.");
        return scenes;
    }

    private static string Env(string name) => (Environment.GetEnvironmentVariable(name) ?? "").Trim();

    private static string GetArg(string name)
    {
        var args = Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++) if (args[i] == name) return args[i + 1];
        return null;
    }
}
#endif