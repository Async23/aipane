#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>

static NSString *BundleString(NSString *key, NSString *fallback) {
    id value = [NSBundle.mainBundle objectForInfoDictionaryKey:key];
    return [value isKindOfClass:NSString.class] && [value length] > 0 ? value : fallback;
}

static NSString *NotifierName(void) {
    return BundleString(@"CFBundleDisplayName", BundleString(@"CFBundleName", @"Agent Notifier"));
}

static NSString *DefaultTitle(void) {
    NSString *name = NotifierName();
    NSString *suffix = @" Notifier";
    return [name hasSuffix:suffix] ? [name substringToIndex:name.length - suffix.length] : name;
}

static NSString *ExecuteKey(void) {
    return [(NSBundle.mainBundle.bundleIdentifier ?: @"agent-notifier") stringByAppendingString:@".execute"];
}

static void PrintUsage(void) {
    printf("%s sends notifications through UNUserNotificationCenter.\n", NotifierName().UTF8String);
    puts("");
    printf("Usage: %s -message VALUE [options]\n", BundleString(@"CFBundleExecutable", @"agent-notifier").UTF8String);
    puts("");
    puts("Options:");
    puts("  -title VALUE");
    puts("  -subtitle VALUE");
    puts("  -message VALUE");
    puts("  -sound NAME");
    puts("  -group ID");
    puts("  -execute COMMAND");
    puts("  -remove ID|ALL");
    puts("  -authorize    Request notification permission without sending a notification");
    puts("  -status       Print notification authorization and presentation settings as JSON");
    puts("  -dry-run");
    puts("  -help");
    puts("  -version");
}

static NSDictionary<NSString *, NSString *> *ParseArguments(int argc, const char *argv[]) {
    NSMutableDictionary<NSString *, NSString *> *arguments = [NSMutableDictionary dictionary];
    NSSet<NSString *> *flags = [NSSet setWithArray:@[@"-help", @"-version", @"-dry-run", @"-authorize", @"-status"]];

    for (int index = 1; index < argc; index++) {
        NSString *key = [NSString stringWithUTF8String:argv[index]];
        if (![key hasPrefix:@"-"]) continue;
        if ([flags containsObject:key]) {
            arguments[key] = @"true";
            continue;
        }
        if (index + 1 < argc) {
            arguments[key] = [NSString stringWithUTF8String:argv[++index]];
        }
    }
    return arguments;
}

static BOOL WaitForFlag(BOOL *flag, NSTimeInterval timeout) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    while (!*flag && deadline.timeIntervalSinceNow > 0) {
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
    }
    return *flag;
}

static int EnsureAuthorization(UNUserNotificationCenter *center) {
    __block BOOL settingsCompleted = NO;
    __block UNNotificationSettings *settings = nil;
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *value) {
        settings = value;
        settingsCompleted = YES;
    }];
    if (!WaitForFlag(&settingsCompleted, 4.0)) {
        fputs("notification settings request timed out\n", stderr);
        return 6;
    }
    if (settings.authorizationStatus == UNAuthorizationStatusDenied) {
        fprintf(stderr, "notifications are disabled for %s\n", NotifierName().UTF8String);
        return 7;
    }
    if (settings.authorizationStatus != UNAuthorizationStatusNotDetermined) return 0;

    __block BOOL authorizationCompleted = NO;
    __block BOOL granted = NO;
    __block NSError *authorizationError = nil;
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound)
                          completionHandler:^(BOOL didGrant, NSError *error) {
        granted = didGrant;
        authorizationError = error;
        authorizationCompleted = YES;
    }];
    if (!WaitForFlag(&authorizationCompleted, 30.0)) {
        fputs("notification authorization request timed out\n", stderr);
        return 8;
    }
    if (authorizationError != nil) {
        fprintf(stderr, "%s\n", authorizationError.localizedDescription.UTF8String);
        return 9;
    }
    if (!granted) {
        fputs("notification authorization was not granted\n", stderr);
        return 10;
    }
    return 0;
}

static void LaunchCommand(NSString *command) {
    if (command.length == 0) return;
    NSTask *task = [NSTask new];
    task.executableURL = [NSURL fileURLWithPath:@"/bin/zsh"];
    task.arguments = @[@"-lc", command];
    task.standardInput = [NSPipe pipe];
    task.standardOutput = [NSPipe pipe];
    task.standardError = [NSPipe pipe];
    [task launchAndReturnError:NULL];
}

@interface AgentNotifierDelegate : NSObject <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@end

@implementation AgentNotifierDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    UNUserNotificationCenter.currentNotificationCenter.delegate = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        [NSApp terminate:nil];
    });
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler {
    (void)center;
    (void)notification;
    completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionSound);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
didReceiveNotificationResponse:(UNNotificationResponse *)response
         withCompletionHandler:(void (^)(void))completionHandler {
    (void)center;
    NSString *command = response.notification.request.content.userInfo[ExecuteKey()];
    LaunchCommand(command);
    completionHandler();
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 200 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [NSApp terminate:nil];
    });
}

@end

static int RemoveNotifications(UNUserNotificationCenter *center, NSString *identifier) {
    if ([identifier isEqualToString:@"ALL"]) {
        [center removeAllPendingNotificationRequests];
        [center removeAllDeliveredNotifications];
    } else {
        [center removePendingNotificationRequestsWithIdentifiers:@[identifier]];
        [center removeDeliveredNotificationsWithIdentifiers:@[identifier]];
    }
    [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.15]];
    return 0;
}

static int SendNotification(NSDictionary<NSString *, NSString *> *arguments,
                            UNUserNotificationCenter *center) {
    NSString *message = arguments[@"-message"];
    if (message.length == 0) {
        fputs("-message is required\n", stderr);
        return 2;
    }
    int authorizationStatus = EnsureAuthorization(center);
    if (authorizationStatus != 0) return authorizationStatus;

    UNMutableNotificationContent *content = [UNMutableNotificationContent new];
    content.title = arguments[@"-title"] ?: DefaultTitle();
    content.subtitle = arguments[@"-subtitle"] ?: @"";
    content.body = message;
    NSString *sound = arguments[@"-sound"];
    if (sound.length > 0) {
        content.sound = [sound isEqualToString:@"default"]
            ? UNNotificationSound.defaultSound
            : [UNNotificationSound soundNamed:sound];
    }
    NSString *command = arguments[@"-execute"];
    if (command.length > 0) content.userInfo = @{ExecuteKey(): command};

    NSString *identifier = arguments[@"-group"] ?: NSUUID.UUID.UUIDString;
    content.threadIdentifier = identifier;
    [center removePendingNotificationRequestsWithIdentifiers:@[identifier]];
    [center removeDeliveredNotificationsWithIdentifiers:@[identifier]];

    UNNotificationRequest *request = [UNNotificationRequest
        requestWithIdentifier:identifier
        content:content
        trigger:nil
    ];
    __block BOOL completed = NO;
    __block NSError *requestError = nil;
    [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
        requestError = error;
        completed = YES;
    }];
    if (!WaitForFlag(&completed, 4.0)) {
        fputs("notification request timed out\n", stderr);
        return 4;
    }
    if (requestError != nil) {
        fprintf(stderr, "%s\n", requestError.localizedDescription.UTF8String);
        return 5;
    }
    [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.25]];
    return 0;
}

static NSString *AuthorizationName(UNAuthorizationStatus status) {
    switch (status) {
        case UNAuthorizationStatusNotDetermined: return @"notDetermined";
        case UNAuthorizationStatusDenied: return @"denied";
        case UNAuthorizationStatusAuthorized: return @"authorized";
        case UNAuthorizationStatusProvisional: return @"provisional";
        default: return @"unknown";
    }
}

static NSString *SettingName(UNNotificationSetting setting) {
    switch (setting) {
        case UNNotificationSettingNotSupported: return @"notSupported";
        case UNNotificationSettingDisabled: return @"disabled";
        case UNNotificationSettingEnabled: return @"enabled";
        default: return @"unknown";
    }
}

static NSString *AlertStyleName(UNAlertStyle style) {
    switch (style) {
        case UNAlertStyleNone: return @"none";
        case UNAlertStyleBanner: return @"banner";
        case UNAlertStyleAlert: return @"alert";
        default: return @"unknown";
    }
}

static int PrintNotificationStatus(UNUserNotificationCenter *center) {
    __block BOOL completed = NO;
    __block UNNotificationSettings *settings = nil;
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *value) {
        settings = value;
        completed = YES;
    }];
    if (!WaitForFlag(&completed, 4.0)) {
        fputs("notification settings request timed out\n", stderr);
        return 6;
    }
    NSDictionary *status = @{
        @"bundleIdentifier": NSBundle.mainBundle.bundleIdentifier ?: @"",
        @"appPath": NSBundle.mainBundle.bundlePath,
        @"authorizationStatus": AuthorizationName(settings.authorizationStatus),
        @"authorizationStatusCode": @(settings.authorizationStatus),
        @"alertStyle": AlertStyleName(settings.alertStyle),
        @"alertSetting": SettingName(settings.alertSetting),
        @"soundSetting": SettingName(settings.soundSetting),
        @"badgeSetting": SettingName(settings.badgeSetting),
        @"notificationCenterSetting": SettingName(settings.notificationCenterSetting),
        @"lockScreenSetting": SettingName(settings.lockScreenSetting),
        @"api": @"UNUserNotificationCenter",
        @"icon": @"bundle"
    };
    NSError *error = nil;
    NSData *json = [NSJSONSerialization dataWithJSONObject:status options:NSJSONWritingSortedKeys error:&error];
    if (json == nil) {
        fprintf(stderr, "%s\n", error.localizedDescription.UTF8String);
        return 11;
    }
    printf("%s\n", [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding].UTF8String);
    return 0;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSDictionary<NSString *, NSString *> *arguments = ParseArguments(argc, argv);
        if (arguments[@"-help"] != nil) {
            PrintUsage();
            return 0;
        }
        if (arguments[@"-version"] != nil) {
            printf("%s %s\n", NotifierName().UTF8String,
                   BundleString(@"CFBundleShortVersionString", @"unknown").UTF8String);
            return 0;
        }
        if (arguments[@"-dry-run"] != nil && arguments[@"-message"] != nil) {
            NSString *identifier = arguments[@"-group"] ?: @"dry-run";
            printf("identifier=%s\nicon=bundle\napi=UNUserNotificationCenter\n",
                   identifier.UTF8String);
            return 0;
        }

        NSApplication *application = NSApplication.sharedApplication;
        application.activationPolicy = NSApplicationActivationPolicyAccessory;
        AgentNotifierDelegate *delegate = [AgentNotifierDelegate new];
        application.delegate = delegate;
        UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
        center.delegate = delegate;

        if (arguments[@"-authorize"] != nil) {
            int result = EnsureAuthorization(center);
            return result == 0 ? PrintNotificationStatus(center) : result;
        }
        if (arguments[@"-status"] != nil) return PrintNotificationStatus(center);

        NSString *removeIdentifier = arguments[@"-remove"];
        if (removeIdentifier.length > 0) return RemoveNotifications(center, removeIdentifier);
        if (arguments[@"-message"] != nil) return SendNotification(arguments, center);
        [application run];
    }
    return 0;
}
