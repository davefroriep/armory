import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.froriep.armoryfit',
  appName: 'Armory Fit',
  webDir: 'dist',
  android: { backgroundColor: '#000000', allowMixedContent: true },
};

export default config;
