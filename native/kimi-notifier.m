#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>

static NSString *const KimiNotifierVersion = @"1.0.0";
static NSString *const KimiExecuteKey = @"com.alfheim.kimi-code-notifier.execute";

static void PrintUsage(void) {
    puts("Kimi Code Notifier sends notifications through UNUserNotificationCenter.");
    puts("");
    puts("Usage: kimi-notifier -message VALUE [options]");
    puts("");
    puts("Options:");
    puts("  -title VALUE");
    puts("  -subtitle VALUE");
    puts("  -message VALUE");
    puts("  -sound NAME");
    puts("  -group ID");
    puts("  -execute COMMAND");
    puts("  -remove ID|ALL");
    puts("  -dry-run");
    puts("  -help");
    puts("  -version");
}

static NSDictionary<NSString *, NSString *> *ParseArguments(int argc, const char *argv[]) {
    NSMutableDictionary<NSString *, NSString *> *arguments = [NSMutableDictionary dictionary];
    NSSet<NSString *> *flags = [NSSet setWithArray:@[@"-help", @"-version", @"-dry-run"]];

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
        fputs("notifications are disabled for Kimi Code Notifier\n", stderr);
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
    [task launchAndReturnError:nil];
}

@interface KimiNotifierDelegate : NSObject <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@end

@implementation KimiNotifierDelegate

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
    NSString *command = response.notification.request.content.userInfo[KimiExecuteKey];
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
    content.title = arguments[@"-title"] ?: @"Kimi Code";
    content.subtitle = arguments[@"-subtitle"] ?: @"";
    content.body = message;
    NSString *sound = arguments[@"-sound"];
    if (sound.length > 0) {
        content.sound = [sound isEqualToString:@"default"]
            ? UNNotificationSound.defaultSound
            : [UNNotificationSound soundNamed:sound];
    }
    NSString *command = arguments[@"-execute"];
    if (command.length > 0) content.userInfo = @{KimiExecuteKey: command};

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

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSDictionary<NSString *, NSString *> *arguments = ParseArguments(argc, argv);
        if (arguments[@"-help"] != nil) {
            PrintUsage();
            return 0;
        }
        if (arguments[@"-version"] != nil) {
            printf("Kimi Code Notifier %s\n", KimiNotifierVersion.UTF8String);
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
        KimiNotifierDelegate *delegate = [KimiNotifierDelegate new];
        application.delegate = delegate;
        UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
        center.delegate = delegate;

        NSString *removeIdentifier = arguments[@"-remove"];
        if (removeIdentifier.length > 0) return RemoveNotifications(center, removeIdentifier);
        if (arguments[@"-message"] != nil) return SendNotification(arguments, center);
        [application run];
    }
    return 0;
}
