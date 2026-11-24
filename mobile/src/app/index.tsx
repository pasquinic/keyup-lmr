import { Redirect } from 'expo-router';
import React from 'react';
import { ActivityIndicator, View } from 'react-native';

import { useSession } from '../session';
import { C } from '../theme';

export default function Index() {
  const { ready, creds } = useSession();
  if (!ready) {
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, justifyContent: 'center' }}>
        <ActivityIndicator color={C.accent} />
      </View>
    );
  }
  return <Redirect href={creds ? '/radio' : '/activate'} />;
}
