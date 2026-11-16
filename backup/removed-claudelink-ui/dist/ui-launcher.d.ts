/**
 * Spawn the UI server as a detached process if one isn't already running,
 * and open the browser. Safe to call repeatedly.
 *
 * Returns the URL of the running UI, or null if it couldn't be started.
 */
export declare function launchUIIfNeeded(opts?: {
    openBrowser?: boolean;
}): Promise<string | null>;
export declare function stopUI(): boolean;
export declare function getUIStatus(): {
    running: boolean;
    pid?: number;
    port?: number;
    url?: string;
};
